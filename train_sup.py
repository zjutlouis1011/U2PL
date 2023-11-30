import argparse
import json
import logging
import os
import os.path as osp
import pprint
import random
import time
from datetime import datetime

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.nn.functional as F
import yaml
from tensorboardX import SummaryWriter
from tqdm import tqdm

from u2pl.dataset.builder import get_loader
from u2pl.models.model_helper import ModelBuilder
from u2pl.utils.dist_helper import setup_distributed
from u2pl.utils.loss_helper import get_criterion
from u2pl.utils.lr_helper import get_optimizer, get_scheduler
from u2pl.utils.utils import (
    AverageMeter,
    get_rank,
    get_world_size,
    init_log,
    intersectionAndUnion,
    load_state,
    set_random_seed,
)

parser = argparse.ArgumentParser(description="Semi-Supervised Semantic Segmentation")
parser.add_argument("--config", type=str, default="config.yaml")
parser.add_argument("--resume", action='store_true')
parser.add_argument("--local_rank", type=int, default=0)
parser.add_argument("--seed", type=int, default=1)
parser.add_argument("--port", default=None, type=int)
parser.add_argument("--pretrain_path", default='', type=str)
logger = init_log("global", logging.INFO)
logger.propagate = 0


def main():
    from loguru import logger as loger
    global args, cfg
    args = parser.parse_args()
    seed = args.seed
    cfg = yaml.load(open(args.config, "r"), Loader=yaml.Loader)
    # loger.info(args.port)

    cfg["exp_path"] = cfg["saver"]["exp_path"]
    cfg["log_path"] = cfg["saver"]["log_path"]
    cfg["task_id"] = osp.join(cfg["dataset"]["type"], cfg["saver"]["task_name"])
    cfg["save_path"] = osp.join(cfg["exp_path"], cfg["task_id"])
    cfg["log_save_path"] = osp.join(cfg["log_path"], cfg["task_id"])

    # cfg["resume"] = True if args.resume else False

    if args.pretrain_path != '':
        cfg["trainer"]["pretrain"] = args.pretrain_path

    cudnn.enabled = True
    cudnn.benchmark = True

    rank, word_size = setup_distributed(port=args.port, backend='gloo')

    if rank == 0:
        logger.info("{}".format(pprint.pformat(cfg)))
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        tb_logger = SummaryWriter(
            osp.join(cfg["log_save_path"], current_time)
        )
    else:
        tb_logger = None

    if args.seed is not None:
        print("set random seed to", args.seed)
        set_random_seed(args.seed)

    if rank == 0:
        os.makedirs(cfg["save_path"], exist_ok=True)
        os.makedirs(cfg["log_save_path"], exist_ok=True)
    model_path = None

    if cfg['trainer']['naic_path'] != '':
        model_path = cfg['trainer']['naic_path']
        from u2pl.naic.deeplabv3_plus import DeepLabv3_plus
        import torch.nn as nn
        model = DeepLabv3_plus(in_channels=3, num_classes=cfg["net"]["num_classes"], backend='resnet101', os=16,
                               pretrained=False, norm_layer=nn.BatchNorm2d)
        if not args.resume:
            loger.info(f"Model from NAIC DeeplabV3Plus from {model_path}..........")
            load_state(model_path, model, key="naic")
        # model.load_state_dict(torch.load(model_path,  map_location='cpu'), strict=False)
        modules_back = [model.backend]
        modules_head = [model.aspp_pooling, model.cbr_low, model.cbr_last]

    else:
        # Create network.
        model = ModelBuilder(cfg["net"])
        modules_back = [model.encoder]
        if cfg["net"].get("aux_loss", False):
            modules_head = [model.auxor, model.decoder]
        else:
            modules_head = [model.decoder]

        if cfg["net"].get("sync_bn", True):
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    model.cuda()

    local_rank = int(os.environ["LOCAL_RANK"])
    model = torch.nn.parallel.DistributedDataParallel(
        model,
        device_ids=[local_rank],
        output_device=local_rank,
        find_unused_parameters=False,
    )

    criterion = get_criterion(cfg)

    train_loader_sup, val_loader = get_loader(cfg, seed=seed)

    # Optimizer and lr decay scheduler
    cfg_trainer = cfg["trainer"]
    cfg_optim = cfg_trainer["optimizer"]
    times = 10  # if "pascal" in cfg["dataset"]["type"] else 1  # 这里要修改

    params_list = []
    for module in modules_back:
        params_list.append(
            dict(params=module.parameters(), lr=cfg_optim["kwargs"]["lr"])
        )
    for module in modules_head:
        params_list.append(
            dict(params=module.parameters(), lr=cfg_optim["kwargs"]["lr"] * times)
        )

    optimizer = get_optimizer(params_list, cfg_optim)

    best_prec = 0
    last_epoch = 0
    # if not model_path:
    # auto_resume > pretrain
    if args.resume:
        lastest_model = osp.join(cfg["save_path"], "ckpt.pth")
        if not os.path.exists(lastest_model):
            "No checkpoint found in '{}'".format(lastest_model)
        else:  # resume
            loger.info(f"Resume model from: '{lastest_model}'")
            best_prec, last_epoch = load_state(
                lastest_model, model, optimizer=optimizer, key="model_state"
            )

    elif cfg["trainer"].get("pretrain", False):
        load_state(cfg["trainer"]["pretrain"], model, key="model_state")

    optimizer_old = get_optimizer(params_list, cfg_optim)
    lr_scheduler = get_scheduler(
        cfg_trainer, len(train_loader_sup), optimizer_old, start_epoch=last_epoch
    )
    with open(osp.join(cfg["save_path"], 'config.yaml'), 'w', encoding='utf-8') as yf:
        yaml.dump(cfg, yf)

    # Start to train model
    CLASSES_need = {
        1: "liangshiqu",
        2: "liaohuang",
        3: "feiliangqu"
    }
    for epoch in range(last_epoch, cfg_trainer["epochs"]):
        # prec, iou_cls = validate(model, val_loader, epoch)
        # Training
        train(
            model,
            optimizer,
            lr_scheduler,
            criterion,
            train_loader_sup,
            epoch,
            tb_logger,
        )

        # Validation and store checkpoint
        prec, iou_cls = validate(model, val_loader, epoch)

        if rank == 0:
            state = {
                "epoch": epoch,
                "model_state": model.state_dict(),
                "optimizer_state": optimizer.state_dict(),
                "best_miou": best_prec,
            }

            if prec > best_prec:
                best_prec = prec
                state["best_miou"] = prec
                torch.save(
                    state, osp.join(cfg["save_path"], "ckpt_best.pth")
                )
                # 只保留权重，不保留优化器等
                torch.save(model.state_dict(), osp.join(cfg["save_path"], "best_model.pth"))

            torch.save(state, osp.join(cfg["save_path"], "ckpt.pth"))
            # write the model_param.json
            now = datetime.now()

            # 格式化日期和时间
            datetime_begin = now.strftime("%Y-%m-%d")
            model_type = cfg["dataset"]["type"].higher()
            model_param_dict = {"modelName": f"DeepLabV3Plus-{model_type}-01",
                                "baseModel": "DeepLabV3Plus",
                                "backbone": "resnet101",
                                "modelType": "landcover-classfication",
                                "modelVersion": "1.0.0",
                                "modelDescription": "模型说明",
                                "category": list(CLASSES_need.values()),
                                "Accuray": round(best_prec * 100, 2),
                                "author": "...",
                                "create-time": datetime_begin,
                                # "end-time": datetime_end
                                }

            with open(os.path.join(cfg["save_path"], 'model_param.json'), 'w', encoding='utf-8') as ff:
                json.dump(model_param_dict, ff, indent=4, ensure_ascii=False)

            logger.info(
                "\033[31m * Currently, the best val result is: {:.2f}\033[0m".format(
                    best_prec * 100
                )
            )
            tb_logger.add_scalar("mIoU val", prec, epoch)
            for i, iou in enumerate(iou_cls):
                tb_logger.add_scalar(f"IoU val{CLASSES_need[i+1]}", iou, epoch)

def train(
    model,
    optimizer,
    lr_scheduler,
    criterion,
    data_loader,
    epoch,
    tb_logger,
):
    model.train()

    data_loader.sampler.set_epoch(epoch)
    data_loader_iter = iter(data_loader)

    rank, world_size = dist.get_rank(), dist.get_world_size()

    losses = AverageMeter(10)
    data_times = AverageMeter(10)
    batch_times = AverageMeter(10)
    learning_rates = AverageMeter(10)

    batch_end = time.time()
    for step in range(len(data_loader)):
        batch_start = time.time()
        data_times.update(batch_start - batch_end)

        i_iter = epoch * len(data_loader) + step
        lr = lr_scheduler.get_lr()
        learning_rates.update(lr[0])
        lr_scheduler.step()

        image, label = next(data_loader_iter)
        batch_size, h, w = label.size()
        image, label = image.cuda(), label.cuda()
        outs = model(image)
        pred = outs["pred"]
        pred = F.interpolate(pred, (h, w), mode="bilinear", align_corners=True)

        if "aux_loss" in cfg["net"].keys():
            aux = outs["aux"]
            aux = F.interpolate(aux, (h, w), mode="bilinear", align_corners=True)
            loss = criterion([pred, aux], label)
        else:
            loss = criterion(pred, label)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # gather all loss from different gpus
        reduced_loss = loss.clone().detach()
        dist.all_reduce(reduced_loss)
        losses.update(reduced_loss.item())

        batch_end = time.time()
        batch_times.update(batch_end - batch_start)

        if i_iter % 20 == 0 and rank == 0:
            logger.info(
                "Iter [{}/{}]\t"
                "Data {data_time.val:.2f} ({data_time.avg:.2f})\t"
                "Time {batch_time.val:.2f} ({batch_time.avg:.2f})\t"
                "Loss {loss.val:.4f} ({loss.avg:.4f})\t"
                "LR {lr.val:.5f} ({lr.avg:.5f})\t".format(
                    i_iter,
                    cfg["trainer"]["epochs"] * len(data_loader),
                    data_time=data_times,
                    batch_time=batch_times,
                    loss=losses,
                    lr=learning_rates,
                )
            )

            tb_logger.add_scalar("lr", learning_rates.avg, i_iter)
            tb_logger.add_scalar("Loss", losses.avg, i_iter)


def validate(
        model,
        data_loader,
        epoch,
):
    model.eval()
    data_loader.sampler.set_epoch(epoch)

    num_classes, ignore_label = (
        cfg["net"]["num_classes"],
        cfg["dataset"]["ignore_label"],
    )
    rank, world_size = dist.get_rank(), dist.get_world_size()

    intersection_meter = AverageMeter()
    union_meter = AverageMeter()

    for batch in tqdm(data_loader):
        images, labels = batch
        images = images.cuda()
        labels = labels.long().cuda()
        batch_size, h, w = labels.shape

        with torch.no_grad():
            outs = model(images)

        # get the output produced by model_teacher
        output = outs["pred"]
        output = F.interpolate(output, (h, w), mode="bilinear", align_corners=True)
        output = output.data.max(1)[1].cpu().numpy()
        target_origin = labels.cpu().numpy()

        # start to calculate miou
        intersection, union, target = intersectionAndUnion(
            output, target_origin, num_classes, ignore_label
        )

        # gather all validation information
        reduced_intersection = torch.from_numpy(intersection).cuda()
        reduced_union = torch.from_numpy(union).cuda()
        reduced_target = torch.from_numpy(target).cuda()

        dist.all_reduce(reduced_intersection)
        dist.all_reduce(reduced_union)
        dist.all_reduce(reduced_target)

        intersection_meter.update(reduced_intersection.cpu().numpy())
        union_meter.update(reduced_union.cpu().numpy())

    iou_class = intersection_meter.sum / (union_meter.sum + 1e-10)
    mIoU = np.mean(iou_class)

    if rank == 0:
        for i, iou in enumerate(iou_class):
            logger.info(" * class [{}] IoU {:.2f}".format(i, iou * 100))
        logger.info(" * epoch {} mIoU {:.2f}".format(epoch, mIoU * 100))

    return mIoU, iou_class


if __name__ == "__main__":
    main()
