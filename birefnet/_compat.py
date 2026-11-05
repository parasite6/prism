"""What birefnet.py imported from transformers and timm, without them.

transformers alone takes ~11 s to import and only provides two base classes
here; timm isn't installed and only provides three small helpers. Inference
output is identical to the original setup (checked pixel for pixel).
"""
import collections.abc

from torch import nn
from torch.nn.init import trunc_normal_  # noqa: F401  (timm's is the same function)


class PretrainedConfig:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class PreTrainedModel(nn.Module):
    def __init__(self, config, *args, **kwargs):
        super().__init__()

    def post_init(self):
        pass


def to_2tuple(x):
    return tuple(x) if isinstance(x, collections.abc.Iterable) and not isinstance(x, str) else (x, x)


class DropPath(nn.Module):
    """Stochastic depth (training only); the identity at inference."""
    def __init__(self, drop_prob=0.0, scale_by_keep=True):
        super().__init__()
        self.drop_prob, self.scale_by_keep = drop_prob, scale_by_keep

    def forward(self, x):
        if self.drop_prob == 0.0 or not self.training:
            return x
        keep = 1 - self.drop_prob
        mask = x.new_empty((x.shape[0],) + (1,) * (x.ndim - 1)).bernoulli_(keep)
        return x * (mask / keep if self.scale_by_keep else mask)
