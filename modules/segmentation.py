import os
from contextlib import nullcontext
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import torch
import tqdm
from monai.data import DataLoader, decollate_batch
from monai.inferers import sliding_window_inference
from monai.losses import DiceCELoss
from monai.metrics import DiceMetric, Metric
from monai.transforms import AsDiscrete
from torch import nn
from torch.nn.modules.loss import _Loss
from torch.optim import SGD, Adam, AdamW
from torch.utils.data import ConcatDataset
from torch.utils.tensorboard import SummaryWriter
from tqdm.auto import tqdm

from lib.loss.target_adaptive_loss import TargetAdaptiveLoss
from lib.utils.validation import get_output_and_mask
from networks.uxnet3d.network_backbone import UXNETDecoder, UXNETEncoder

crop_sample = 2

torch.backends.cudnn.benchmark = True


class SegmentationModule(nn.Module):
    def __init__(
        self,
        out_channels: int,
        feat_extractor: Optional[nn.Module] = None,
        predictor: Optional[nn.Module] = None,
        net: Optional[nn.Module] = None,
        criterion: _Loss = DiceCELoss(to_onehot_y=True, softmax=True),
        optimizer: str = "AdamW",
        lr: float = 0.0001,
        amp=False,
    ):
        super().__init__()

        self.net = net
        self.feat_extractor = feat_extractor
        self.predictor = predictor

        if net:
            params = self.net.parameters()
            if (feat_extractor is not None) or (predictor is not None):
                raise Warning(
                    "net and (feat_extractor, predictor) are both provided. However, only net will be trained."
                )
        elif (feat_extractor is not None) and (predictor is not None):
            params = list(self.feat_extractor.parameters()) + list(self.predictor.parameters())
        else:
            # Default feature extractor and predictor
            self.feat_extractor = UXNETEncoder(in_chans=1)
            self.predictor = UXNETDecoder(out_chans=out_channels)
            params = list(self.feat_extractor.parameters()) + list(self.predictor.parameters())

        differentiable_params = [p for p in params if p.requires_grad]
        self.criterion = criterion
        self.lr = lr
        if optimizer == "AdamW":
            self.optimizer = AdamW(differentiable_params, lr=self.lr)
        if optimizer == "Adam":
            self.optimizer = Adam(differentiable_params, lr=self.lr)
        if optimizer == "SGD":
            self.optimizer = SGD(differentiable_params, lr=self.lr)
        self.amp = amp

    def forward(self, x):
        if self.net:
            y = self.net(x)
        else:
            feature, skip_outputs = self.feat_extractor(x)
            y = self.predictor((feature, skip_outputs))
        return y

    def update(self, x, y, modality=None):
        self.optimizer.zero_grad()
        with torch.autocast(device_type="cuda") if self.amp else nullcontext():
            output = self.forward(x)
            if modality == 0:
                loss = self.criterion[0](output, y)
            elif modality == 1:
                loss = self.criterion[1](output, y)
            else:
                loss = self.criterion(output, y)
        loss.backward()
        self.optimizer.step()
        return loss.item()

    def inference(self, x, roi_size=(96, 96, 96)):
        # Using sliding windows
        self.eval()
        sw_batch_size = crop_sample  # this is used corresponding to amos transforms
        return sliding_window_inference(x, roi_size, sw_batch_size, self.forward)

    def save(self, checkpoint_dir):
        Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)
        if self.net:
            torch.save(self.net.state_dict(), os.path.join(checkpoint_dir, "net.pth"))
        else:
            torch.save(self.feat_extractor.state_dict(), os.path.join(checkpoint_dir, "feat_extractor_state.pth"))
            torch.save(self.predictor.state_dict(), os.path.join(checkpoint_dir, "predictor_state.pth"))

    def load(self, checkpoint_dir):
        if self.net:
            self.net.load_state_dict(torch.load(os.path.join(checkpoint_dir, "net.pth")))
        else:
            self.feat_extractor.load_state_dict(torch.load(os.path.join(checkpoint_dir, "feat_extractor_state.pth")))
            self.predictor.load_state_dict(torch.load(os.path.join(checkpoint_dir, "predictor_state.pth")))

    def print_info(self):
        if self.net:
            print("Module:", self.net.__class__.__name__)
        else:
            print("Module Encoder:", self.feat_extractor.__class__.__name__)
            print("       Decoder:", self.predictor.__class__.__name__)
        print("Optimizer:", self.optimizer.__class__.__name__, f"(lr = {self.lr})")
        print("Loss function:", repr(self.criterion))


class SegmentationTrainer:
    def __init__(
        self,
        num_classes: int,
        max_iter: int = 40000,
        metric: Metric = DiceMetric(include_background=True, reduction="mean", get_not_nans=False),
        eval_step: int = 500,
        checkpoint_dir: str = "./checkpoints/",
        device: Literal["cuda", "cpu"] = "cuda",
        data_info: dict = None,
        partially_labelled: bool = False,
    ):
        self.max_iter = max_iter
        self.num_classes = num_classes
        self.metric = metric
        self.eval_step = eval_step
        self.checkpoint_dir = checkpoint_dir
        self.device = device
        self.data_info = data_info
        self.partially_labelled = partially_labelled

    # auxilary function to show training info before training procedure starts
    def show_training_info(self, module, train_dataloader, val_dataloader):
        print("--------")
        print("Device:", self.device)  # device is a global variable (not an argument of cli)
        print("# of Training Samples:", len(train_dataloader))
        print("# of Validation Samples:", len(val_dataloader))
        print("Max iteration:", self.max_iter, f"steps (validates per {self.eval_step} steps)")
        print("Checkpoint directory:", self.checkpoint_dir)
        print("Evaluation metric:", self.metric.__class__.__name__)
        module.print_info()
        print("--------")

    def validation(self, module, dataloader, global_step=None, **kwargs):
        module.eval()
        val_metrics = []
        val_pbar = tqdm(dataloader, dynamic_ncols=True)
        metric_name = self.metric.__class__.__name__
        train_val_desc = (
            "Validate ({} Steps) (Partially-labelled:{}) ({}={:2.5f})"  # progress bar description used during training
        )
        simple_val_desc = (
            "Validate (Partially-labelled:{}) ({}={:2.5f})"  # progress bar description used when the network is tested
        )
        val_on_partial = self.partially_labelled and (global_step is not None)
        with torch.no_grad():
            for batch in val_pbar:
                # Infer, decollate data into list of samples, and proprocess both predictions and labels
                images, masks = batch["image"].to(self.device), batch["label"].to(self.device)
                modality = batch["modality"][0]
                infer_out = module.inference(images)
                samples = decollate_batch({"prediction": infer_out, "ground_truth": masks})
                if self.partially_labelled and (global_step is not None):
                    background_class = list(self.data_info["bg"][modality].keys())
                else:
                    background_class = None
                outputs, masks = get_output_and_mask(samples, self.num_classes, background_class)
                # Compute validation metrics
                self.metric(y_pred=outputs, y=masks)
                batch_metric = self.metric.aggregate().item()
                val_metrics.append(batch_metric)
                self.metric.reset()
                # Update progressbar
                if global_step is not None:
                    val_pbar.set_description(
                        train_val_desc.format(global_step, val_on_partial, metric_name, batch_metric)
                    )
                else:
                    val_pbar.set_description(simple_val_desc.format(val_on_partial, metric_name, batch_metric))
        mean_val_metric = np.mean(val_metrics)
        return mean_val_metric

    def train(self, module, train_dataloader, val_dataloader):
        self.show_training_info(module, train_dataloader, val_dataloader)
        best_metric = 0
        train_pbar = tqdm(range(self.max_iter), dynamic_ncols=True)
        writer = SummaryWriter(log_dir=self.checkpoint_dir)
        writer.add_scalar(f"train/{self.metric.__class__.__name__}", 0, 0)  # validation metric starts from zero
        for step in train_pbar:
            module.train()
            # Backpropagation
            batch = next(iter(train_dataloader))
            images = batch["image"].to(self.device)
            masks = batch["label"].to(self.device)
            modality_label = batch["modality"]
            if self.partially_labelled:
                modality = 0 if modality_label == "ct" else 1
            else:
                modality = None
            loss = module.update(images, masks, modality)
            train_pbar.set_description(f"Training ({step+1} / {self.max_iter} Steps) (loss={loss:2.5f})")
            writer.add_scalar(f"train/{module.criterion.__class__.__name__}", loss, step)
            # Validation
            if ((step + 1) % self.eval_step == 0) or (step == self.max_iter - 1):
                val_metric = self.validation(module, val_dataloader, global_step=step + 1)
                writer.add_scalar(f"train/{self.metric.__class__.__name__}", val_metric, step)
                if val_metric > best_metric:
                    module.save(self.checkpoint_dir)
                    tqdm.write(f"Model saved! Validation: (New) {val_metric:2.7f} > (Old) {best_metric:2.7f}")
                    best_metric = val_metric
                else:
                    tqdm.write(f"No improvement. Validation: (New) {val_metric:2.7f} <= (Old) {best_metric:2.7f}")


def concat(dataset):
    if dataset[0] and dataset[1]:
        return ConcatDataset(dataset)
    elif dataset[0]:
        return dataset[0]
    elif dataset[1]:
        return dataset[1]
    else:
        raise ValueError("Either index 0 or 1 should be valid dataset.")


class SegmentationInitializer:
    @staticmethod
    def init_dataloaders(train_dataset, val_dataset, test_dataset, batch_size, dev):
        train_dataloader = DataLoader(concat(train_dataset), batch_size=batch_size, shuffle=~dev, pin_memory=True)
        val_dataloader = DataLoader(concat(val_dataset), batch_size=1, shuffle=False, pin_memory=True)
        if any(test_dataset):
            test_dataloader = DataLoader(concat(test_dataset), batch_size=1, shuffle=False, pin_memory=True)
        else:
            test_dataloader = None

        return train_dataloader, val_dataloader, test_dataloader

    @staticmethod
    def init_module(out_channels, loss, optim, lr, data_info, modality, partially_labelled, device, **kwargs):
        if loss != "tal":
            criterion = DiceCELoss(include_background=True, to_onehot_y=True, softmax=True)
        elif modality in ["ct", "mr"]:
            criterion = TargetAdaptiveLoss(data_info["num_classes"], data_info["fg"][modality], device)
        else:
            ct_criterion = TargetAdaptiveLoss(data_info["num_classes"], data_info["fg"]["ct"], device)
            mr_criterion = TargetAdaptiveLoss(data_info["num_classes"], data_info["fg"]["mr"], device)
            criterion = (ct_criterion, mr_criterion)
        module = SegmentationModule(
            out_channels=out_channels,
            optimizer=optim,
            lr=lr,
            criterion=criterion,
            **kwargs,
        )
        return module

    @staticmethod
    def init_trainer(num_classes, max_iter, eval_step, checkpoint_dir, device, data_info, partially_labelled, **kwargs):
        trainer = SegmentationTrainer(
            num_classes=num_classes,
            max_iter=max_iter,
            eval_step=eval_step,
            checkpoint_dir=checkpoint_dir,
            device=device,
            data_info=data_info,
            partially_labelled=partially_labelled,
        )
        return trainer