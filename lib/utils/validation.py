from medaset.transforms import BackgroundifyClasses
from monai.transforms import AsDiscrete, Compose


def get_output_and_mask(samples, num_classes, background=None):
    if background:
        postprocess = {
            "x": Compose(
                AsDiscrete(argmax=True, to_onehot=num_classes),
                BackgroundifyClasses(channel_dim=0, classes=background),
            ),
            "y": Compose(
                AsDiscrete(to_onehot=num_classes),
                BackgroundifyClasses(channel_dim=0, classes=background),
            ),
        }
    else:
        postprocess = {
            "x": AsDiscrete(argmax=True, to_onehot=num_classes),
            "y": AsDiscrete(to_onehot=num_classes),
        }

    outputs = [postprocess["x"](sample["prediction"]) for sample in samples]
    masks = [postprocess["y"](sample["ground_truth"]) for sample in samples]
    return outputs, masks
