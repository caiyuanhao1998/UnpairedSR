import torch
from torch import nn
import torch.nn.functional as F

from utils.registry import ARCH_REGISTRY
from .edsr import ResBlock, default_conv
from kornia.color import yuv


@ARCH_REGISTRY.register()
class DegModel(nn.Module):
    def __init__(self, nf, nb, in_nc=3, ksize=21, scale=4, jpeg=False, noise=False):
        super().__init__()

        self.ksize = ksize
        self.scale = scale
        self.jpeg = jpeg
        self.noise = noise

        last_channel_num = ksize ** 2 
        if self.jpeg:
            last_channel_num += 1
        if self.noise:
            last_channel_num += 1

        deg_module = [
            nn.Conv2d(in_nc, nf, 3, 1, 1),
            *[
                ResBlock(
                    conv=default_conv, n_feat=nf, kernel_size=3
                    ) for _ in range(nb)
                ],
            nn.Conv2d(nf, last_channel_num, 1, 1, 0),
        ]
        self.deg_module = nn.Sequential(*deg_module)

        nn.init.constant_(self.deg_module[-1].weight, 0)
        nn.init.constant_(self.deg_module[-1].bias, 0)

        # self.deg_module[-1].weight.data[ksize**2:] = 0
        self.deg_module[-1].bias.data[ksize**2//2] = 1

        self.pad = nn.ReflectionPad2d(self.ksize//2)
        
    def forward(self, x, z):
        B, C, H, W = x.shape

        deg_param = self.deg_module(z)

        # kernel
        kernel = deg_param[:, :self.ksize**2].view(
            B, 1, self.ksize**2, *z.shape[2:]
            )
        kernel = kernel / (kernel.sum(dim=2, keepdim=True) + 1e-16)

        x = x.view(B*C, 1, H, W)
        x = F.unfold(
            self.pad(x), kernel_size=self.ksize, stride=self.scale, padding=0
        ).view(B, C, self.ksize**2, *z.shape[2:])
        x = torch.mul(x, kernel).sum(2).view(B, C, *z.shape[2:])
        kernel = kernel.view(B, self.ksize**2, *z.shape[2:])

        # noise
        if self.noise:
            noise_std = deg_param[:, self.ksize**2:self.ksize**2+1]
            # noise_std = noise_std.mean([2, 3], keepdim=True)
            noise = noise_std * torch.randn_like(z)
            x = x + noise
        else:
            noise = None
        
        # jpeg
        if self.jpeg:
            y, u, v = yuv.rgb_to_yuv(x).chunk(3, dim=1)
            y = torch.fft.fft2(y)
            jpeg = deg_param[:, self.ksize**2+1:]
            y = y + jpeg
            y = torch.fft.ifft2(y).real()
            x = yuv.yuv_to_rgb(torch.cat([y, u, v], dim=1))
        else:
            jpeg = None
        
        return x, kernel, noise, jpeg
