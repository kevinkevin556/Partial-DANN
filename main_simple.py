import argparse
import json
from datetime import datetime
from pathlib import Path

import torch
from medaset.amos import (
    SimpleAMOSDataset,
    simple_amos_train_transforms,
    simple_amos_val_transforms,
)
from monai.data import DataLoader
from monai.losses import DiceCELoss
from monai.utils import set_determinism
from torch.utils.data import ConcatDataset

from dann import DANNModule, DANNTrainer
from lib.loss.target_adaptive_loss import TargetAdaptiveLoss
from segmentation import SegmentationModule, SegmentationTrainer

device = torch.device("cuda")
crop_sample = 2

# background and foreground for both modalities (if --masked is applied)
bg = {
    "ct": {i: 0 for i in range(1, SimpleAMOSDataset.num_classes) if i % 2 == 0},
    "mr": {i: 0 for i in range(1, SimpleAMOSDataset.num_classes) if i % 2 == 1},
}
fg = {
    "ct": [i for i in range(1, SimpleAMOSDataset.num_classes) if i % 2 == 1],
    "mr": [i for i in range(1, SimpleAMOSDataset.num_classes) if i % 2 == 0],
}

module_dict = {
    "segmentation": SegmentationModule,
    "dann": DANNModule,
}

trainer_dict = {
    "segmentation": SegmentationTrainer,
    "dann": DANNTrainer,
}


def split_train_data(modality: str, bg_mapping: dict, data_config: dict):
    _configs = data_config.copy()
    _configs["modality"] = modality
    ## Training set and validation set are generated by spliting the original training set.
    ## Testing set is the validation set from the original data.
    ## ** note: We test the network without masking any annotation of the given organs.
    ##          Thus mask_mapping is assigned None.
    train_dataset = SimpleAMOSDataset(
        stage="train", transform=simple_amos_train_transforms, mask_mapping=bg_mapping, **_configs
    )
    val_dataset = SimpleAMOSDataset(
        stage="train", transform=simple_amos_val_transforms, mask_mapping=bg_mapping, **_configs
    )
    test_dataset = SimpleAMOSDataset(
        stage="validation", transform=simple_amos_val_transforms, mask_mapping=None, **_configs
    )
    # 10% of the original training data is used as validation set.
    train_dataset = train_dataset[: -int(len(train_dataset) * 0.1)]
    val_dataset = val_dataset[-int(len(val_dataset) * 0.1) :]
    return train_dataset, val_dataset, test_dataset


def get_args():
    parser = argparse.ArgumentParser(description="Segementation branch of DANN, using SimpleAMOS dataset.")

    # Input data hyperparameters
    parser.add_argument("--root", type=str, default="", required=True, help="Root folder of all your images and labels")
    parser.add_argument("--output", type=str, default="checkpoints", help="Output folder for the best model")
    parser.add_argument("--modality", type=str, default="ct", help="Modality type: ct / mr / ct+mr")
    parser.add_argument("--masked", action="store_true", help="If true, train with annotation-masked data")

    parser.add_argument(
        "--train_data",
        type=str,
        required=True,
        help="Training data: 'all' (train data)) / 'split' (into training & val sets)",
    )

    # Training module hyperparameters
    parser.add_argument("--mode", type=str, default="train", help="Mode: train / test")
    parser.add_argument("--module", type=str, required=True, help="Module: segmentation / dann")
    parser.add_argument(
        "--pretrained", type=str, help="Checkpointing dir for testing or continuing training procedure."
    )
    parser.add_argument("--batch_size", type=int, default="1", help="Batch size for subject input")
    parser.add_argument("--loss", type=str, default="dice2", help="Loss: dice2 (=DiceCE) / tal")
    parser.add_argument("--lr", type=float, default=0.0001, help="Learning rate for training")
    parser.add_argument("--optim", type=str, default="AdamW", help="Optimizer types: Adam / AdamW")
    parser.add_argument("--max_iter", type=int, default=40000, help="Maximum iteration steps for training")
    parser.add_argument("--eval_step", type=int, default=500, help="Per steps to perform validation")
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument(
        "--dev",
        action="store_true",
        help=(
            "Developer mode.",
            "Only a small fraction of data are loaded,",
            "the train_dataloader is not shuffled,",
            "and temp checkpoints are saved in the directory 'debug/'",
        ),
    )
    # Efficiency hyperparameters
    parser.add_argument("--cache_rate", type=float, default=0.1, help="Cache rate to cache your dataset into GPUs")
    parser.add_argument("--num_workers", type=int, default=2, help="Number of workers")
    args = parser.parse_args()
    return args


def get_datasets(args):
    data_config = {
        "root_dir": args.root,
        "modality": args.modality,
        "cache_rate": args.cache_rate,
        "num_workers": args.num_workers,
        "dev": args.dev,
    }

    print("** Modality =", args.modality)
    print("** Training set =", args.train_data)

    if args.train_data == "all":
        if not args.masked:
            print("** Foreground =", list(range(1, SimpleAMOSDataset.num_classes)))
            train_dataset = SimpleAMOSDataset(stage="train", mask_mapping=None, **data_config)
            val_dataset = SimpleAMOSDataset(stage="validation", mask_mapping=None, **data_config)
            test_dataset = None
        else:
            # Annotation masked
            print("** Annotation masked = True")
            if args.modality in ["ct", "mr"]:
                print("** Foreground =", fg[args.modality])
                train_dataset = SimpleAMOSDataset(stage="train", mask_mapping=bg[args.modality], **data_config)
                val_dataset = SimpleAMOSDataset(stage="validation", mask_mapping=bg[args.modality], **data_config)
                test_dataset = None
            else:
                print("** Foreground =\n  - ct:", fg["ct"], "\n  - mr:", fg["mr"])
                # Read ct and mr data respectively
                data_config["modality"] = "ct"
                ct_train_dataset = SimpleAMOSDataset(stage="train", mask_mapping=bg["ct"], **data_config)
                ct_val_dataset = SimpleAMOSDataset(stage="validation", mask_mapping=bg["ct"], **data_config)
                data_config["modality"] = "mr"
                mr_train_dataset = SimpleAMOSDataset(stage="train", mask_mapping=bg["mr"], **data_config)
                mr_val_dataset = SimpleAMOSDataset(stage="validation", mask_mapping=bg["mr"], **data_config)
                # Combine ct and mr data into training and validation set
                train_dataset = ConcatDataset([ct_train_dataset, mr_train_dataset])
                val_dataset = ConcatDataset([ct_val_dataset, mr_val_dataset])
                test_dataset = None
    elif args.train_data == "split":
        if args.modality in ["ct", "mr"]:
            if args.masked:
                print("** Annotation masked = True")
                print("** Foreground =", fg[args.modality])
                bg_mapping = bg[args.modality]
            else:
                print("** Foreground =", list(range(1, SimpleAMOSDataset.num_classes)))
                bg_mapping = None
            train_dataset, val_dataset, test_dataset = split_train_data(args.modality, bg_mapping, data_config)
        else:
            if args.masked:
                print("** Annotation masked = True")
                print("** Foreground =\n  - ct:", fg["ct"], "\n  - mr:", fg["mr"])
                ct_bg_mapping, mr_bg_mapping = bg["ct"], bg["mr"]
            else:
                print("** Foreground =", list(range(1, SimpleAMOSDataset.num_classes)))
                ct_bg_mapping, mr_bg_mapping = None, None

            ct_train_dataset, ct_val_dataset, ct_test_dataset = split_train_data("ct", ct_bg_mapping, data_config)
            mr_train_dataset, mr_val_dataset, mr_test_dataset = split_train_data("mr", mr_bg_mapping, data_config)
            train_dataset = ConcatDataset([ct_train_dataset, mr_train_dataset])
            val_dataset = ConcatDataset([ct_val_dataset, mr_val_dataset])
            test_dataset = ConcatDataset([ct_test_dataset, mr_test_dataset])
    else:
        raise ValueError("Got an invalid input of option --train_data.")

    if args.module == "segementation":
        return train_dataset, val_dataset, test_dataset
    else:
        return (
            (ct_train_dataset, mr_train_dataset),
            (ct_val_dataset, mr_val_dataset),
            (ct_test_dataset, mr_test_dataset),
        )


def main():
    args = get_args()

    ## Whether train without randomness
    if args.deterministic:
        set_determinism(seed=0)
        # torch.use_deterministic_algorithms(mode=True)
        print("** Deterministic = True")

    ## Dataloaders
    train_dataset, val_dataset, test_dataset = get_datasets(args)
    if args.module == "segmentation":
        train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=~args.dev, pin_memory=True)
        val_dataloader = DataLoader(val_dataset, batch_size=1, shuffle=~args.dev, pin_memory=True)
        test_dataloader = (
            DataLoader(test_dataset, batch_size=1, shuffle=False, pin_memory=True) if test_dataset else None
        )
    else:
        ct_train_dataloader = DataLoader(
            train_dataset[0], batch_size=args.batch_size, shuffle=~args.dev, pin_memory=True
        )
        ct_val_dataloader = DataLoader(val_dataset[0], batch_size=1, shuffle=False, pin_memory=True)
        mr_train_dataloader = DataLoader(
            train_dataset[1], batch_size=args.batch_size, shuffle=~args.dev, pin_memory=True
        )
        mr_val_dataloader = DataLoader(val_dataset[1], batch_size=1, shuffle=False, pin_memory=True)
        ct_test_dataloader = (
            DataLoader(test_dataset[0], batch_size=1, shuffle=False, pin_memory=True) if test_dataset else None
        )
        mr_test_dataloader = (
            DataLoader(test_dataset[1], batch_size=1, shuffle=False, pin_memory=True) if test_dataset else None
        )

    # breakpoint()

    ## Initialize module
    if args.module == "segmentation":
        if args.loss != "tal":
            criterion = DiceCELoss(include_background=True, to_onehot_y=True, softmax=True)
        elif args.modality in ["ct", "mr"]:
            criterion = TargetAdaptiveLoss(SimpleAMOSDataset.num_classes, fg[args.modality], device)
        else:
            raise NotImplementedError("Target adaptive loss does not support ct+mr currently.")

        module = SegmentationModule(optimizer=args.optim, lr=args.lr, criterion=criterion)
    elif args.module == "dann":
        if args.loss != "tal" and not args.masked:
            module = DANNModule(None, None, args.optim, args.lr, SimpleAMOSDataset.num_classes)
        else:
            module = DANNModule(fg["ct"], fg["mr"], args.optim, args.lr, SimpleAMOSDataset.num_classes)
    else:
        module = module_dict[args.module]()

    ## Load module if pretrained checkpoint is provided
    if args.pretrained:
        print("** Pretrained checkpoint =", args.pretrained)
        module.load(args.pretrained)
    module.to(device)

    ## Train or test
    # ** note: temp checkpoints are saved in the "debug" directory
    #          to separate the result of experiments and temporary
    #          checkpoints generated in developer mode.
    checkpoint_dir = args.output if not args.dev else "debug"
    # create subfolder based on time
    checkpoint_dir = Path(checkpoint_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
    trainer = trainer_dict[args.module](
        max_iter=args.max_iter,
        eval_step=args.eval_step,
        checkpoint_dir=checkpoint_dir,
        device=device,
    )

    # breakpoint()

    print("** Mode =", args.mode)
    if args.mode == "train":
        # Save command-line arguments
        Path(trainer.checkpoint_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(trainer.checkpoint_dir) / "args.json", "w") as f:
            json.dump(vars(args), f, indent=4)
        # Train
        if args.module == "segmentation":
            trainer.train(module, train_dataloader, val_dataloader)
            if test_dataloader:
                test_metric = trainer.validation(module, test_dataloader)
                print("** Test (Final):", test_metric)
        else:
            trainer.train(module, ct_train_dataloader, ct_val_dataloader, mr_train_dataloader, mr_val_dataloader)
            if ct_test_dataloader and mr_test_dataloader:
                test_metric = trainer.validation(module, ct_test_dataloader, mr_test_dataloader, label="all")
                print("** Test (Final):", test_metric)
    elif args.mode == "test":
        if test_dataloader or (ct_test_dataloader and mr_test_dataloader):
            if args.module == "segmentation":
                test_metric = trainer.validation(module, test_dataloader)
            else:
                test_metric = trainer.validation(module, ct_test_dataloader, mr_test_dataloader, label="all")
            print("** Test (Final):", test_metric)
    else:
        raise ValueError("Got an invalid input of option --mode.")


if __name__ == "__main__":
    main()
