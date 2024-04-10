from __future__ import annotations

from typing import Literal

import numpy as np
import torch
from torch import ones, zeros

from modules.base.updater import BaseUpdater
from modules.dann.module import DANNModule


class PartUpdaterDANN(BaseUpdater):
    def __init__(
        self,
        sampling_mode: Literal["sequential", "random_swap", "random_choice"] = "sequential",
    ):
        super().__init__()
        self.sampling_mode = sampling_mode

    @staticmethod
    def grl_lambda(step, max_iter):
        p = float(step) / max_iter
        grl_lambda = 2.0 / (1.0 + np.exp(-10 * p)) - 1
        return grl_lambda

    def check_module(self, module):
        assert isinstance(module, torch.nn.Module), "The specified module should inherit torch.nn.Module."
        assert isinstance(module, DANNModule), "The specified module should inherit DANNModule."
        for component in (
            "ct_criterion",
            "mr_criterion",
            "optimizer",
            "feat_extractor",
            "predictor",
            "dom_classifier",
            "grl",
            "adv_loss",
        ):
            assert getattr(
                module, component, False
            ), "The specified module should incoporate component/method: {component}"

    def update(self, module, images, masks, modalities, alpha=1):
        # Set alpha value for the gradient reversal layer and reset gradients
        module.grl.set_alpha(alpha)
        module.optimizer.zero_grad()

        # Extract features and make predictions for CT and MR images
        ct_image, ct_mask = images[0], masks[0]
        mr_image, mr_mask = images[1], masks[1]
        ct_skip_outputs, ct_feature = module.feat_extractor(ct_image)
        ct_output = module.predictor((ct_skip_outputs, ct_feature))
        mr_skip_outputs, mr_feature = module.feat_extractor(mr_image)
        mr_output = module.predictor((mr_skip_outputs, mr_feature))

        # Compute segmentation losses for CT and MR images
        ct_seg_loss = module.ct_criterion(ct_output, ct_mask)
        mr_seg_loss = module.mr_criterion(mr_output, mr_mask)

        # Total segmentation loss is the sum of individual losses
        seg_loss = ct_seg_loss + mr_seg_loss
        seg_loss.backward(retain_graph=True)

        # Compute adversarial loss for domain classification
        ct_dom_pred_logits = module.dom_classifier(module.grl.apply(ct_feature))
        mr_dom_pred_logits = module.dom_classifier(module.grl.apply(mr_feature))
        # Combine domain predictions and true labels
        ct_shape, mr_shape = ct_dom_pred_logits.shape, mr_dom_pred_logits.shape
        dom_pred_logits = torch.cat([ct_dom_pred_logits, mr_dom_pred_logits])
        dom_true_label = torch.cat((ones(ct_shape, device="cuda"), zeros(mr_shape, device="cuda")))
        # Calculate adversarial loss and perform backward pass
        adv_loss = module.adv_loss(dom_pred_logits, dom_true_label)
        adv_loss.backward()

        # Update the model parameters
        module.optimizer.step()
        return seg_loss.item(), adv_loss.item()
