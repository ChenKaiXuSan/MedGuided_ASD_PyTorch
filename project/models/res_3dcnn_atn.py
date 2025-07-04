#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""
File: /workspace/code/project/models/make_model copy.py
Project: /workspace/code/project/models
Created Date: Thursday May 8th 2025
Author: Kaixu Chen
-----
Comment:

Have a good code time :)
-----
Last Modified: Thursday May 8th 2025 1:36:35 pm
Modified By: the developer formerly known as Kaixu Chen at <chenkaixusan@gmail.com>
-----
Copyright (c) 2025 The University of Tsukuba
-----
HISTORY:
Date      	By	Comments
----------	---	---------------------------------------------------------
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

class LateFusionBlock(nn.Module):
    def __init__(self, in_dim: int, out_dim: int):
        super().__init__()
        self.attn_mlp = nn.Sequential(
            nn.Linear(1, out_dim),
            nn.ReLU(),
            nn.Linear(out_dim, out_dim),
        )
        self.alpha = nn.Parameter(torch.tensor(0.5))  # 可学习权重参数 α

    def forward(self, main_feat: torch.Tensor, attn_map: torch.Tensor) -> torch.Tensor:
        """
        Args:
            main_feat: 主干网络输出 (B, out_dim)
            attn_map:  attention map (B, 1, T, H, W)

        Returns:
            torch.Tensor: 融合后的输出 (B, out_dim)
        """
        attn_feat = attn_map.mean(dim=[2, 3, 4])  # (B, 1)
        attn_proj = self.attn_mlp(attn_feat)  # (B, out_dim)
        out = self.alpha * main_feat + (1 - self.alpha) * attn_proj
        return out



class Res3DCNNATN(nn.Module):
    """
    make 3D CNN model with Attention Branch Network.
    https://github.com/machine-perception-robotics-group/attention_branch_network

    """

    def __init__(self, hparams) -> None:
        super().__init__()

        self.model_class_num = hparams.model.model_class_num
        self.fuse_method = hparams.model.fuse_method

        self.stem, self.stage, self.head = self.init_resnet(
            fuse_method=self.fuse_method,
            class_num=self.model_class_num,
        )

        if self.fuse_method == "late":
            self.late_fusion = LateFusionBlock(
                in_dim=self.model_class_num,  # Input dimension from the main feature
                out_dim=self.model_class_num,  # Output dimension for the attention map
            )

        # make self layer
        self.relu = nn.ReLU(inplace=True)

        self.bn_att = nn.BatchNorm3d(2048)
        self.attn_conv = nn.Conv3d(
            2048, self.model_class_num, kernel_size=1, padding=0, bias=False
        )
        self.bn_att2 = nn.BatchNorm3d(self.model_class_num)
        self.attn_conv2 = nn.Conv3d(
            self.model_class_num,
            self.model_class_num,
            kernel_size=1,
            padding=0,
            bias=False,
        )
        self.attn_conv3 = nn.Conv3d(
            self.model_class_num, 1, kernel_size=1, padding=0, bias=False
        )
        self.bn_att3 = nn.BatchNorm3d(1)
        self.att_gap = nn.AdaptiveAvgPool3d((16))  # copy from the original code
        self.sigmoid = nn.Sigmoid()

        self.avgpool = nn.AdaptiveAvgPool3d((8))

    @staticmethod
    def init_resnet(class_num: int = 3, fuse_method: str = "add") -> tuple[nn.Module]:

        slow = torch.hub.load(
            "facebookresearch/pytorchvideo", "slow_r50", pretrained=True
        )

        if fuse_method == "concat":
            input_channel = 3 + 1
        else:
            input_channel = 3

        # for the folw model and rgb model
        slow.blocks[0].conv = nn.Conv3d(
            input_channel,
            64,
            kernel_size=(1, 7, 7),
            stride=(1, 2, 2),
            padding=(0, 3, 3),
            bias=False,
        )
        # change the knetics-400 output 400 to model class num
        slow.blocks[-1].proj = nn.Linear(2048, class_num)

        stem: nn.Module = slow.blocks[0]
        stage: nn.Module = slow.blocks[1:5]
        head: nn.Module = slow.blocks[-1]

        return stem, stage, head

    def forward(
        self, video: torch.Tensor, attn_map: torch.Tensor
    ) -> tuple[torch.Tensor]:

        if self.fuse_method == "concat":
            # video = torch.cat((video, attn_map), dim=1)
            _input = torch.cat([video, attn_map], dim=1)
        elif self.fuse_method == "add":
            _input = video + attn_map
        elif self.fuse_method == "mul":
            _input = video * attn_map
        elif self.fuse_method == "avg":
            _input = (video + attn_map) / 2
        elif self.fuse_method == "none":
            _input = video
        else:
            raise KeyError(
                f"the fuse method {self.fuse_method} is not in the model zoo"
            )

        b, c, t, h, w = _input.size()

        x = self.stem(_input)  # b, 64, 8, 56, 56
        for resstage in self.stage:
            x = resstage(x)  # output: b, 2048, 8, 7, 7

        ax = self.bn_att(x)
        ax = self.relu(self.bn_att2(self.attn_conv(ax)))
        axb, axc, axt, axh, axw = ax.size()

        att = self.sigmoid(self.bn_att3(self.attn_conv3(ax)))  # b, 1, 8, 7, 7
        #
        ax: torch.Tensor = self.attn_conv2(ax)
        ax = self.att_gap(ax)
        ax = ax.view(ax.size(0), -1)
        # pred score
        rx: torch.Tensor = x * att
        rx = rx + x

        rx = self.head(rx)

        # attn map
        res_att = []
        for f in range(t):
            _att = F.interpolate(
                att[:, :, f, ...], size=(h, w), mode="bilinear", align_corners=False
            )
            res_att.append(_att)

        return ax, rx, torch.stack(res_att, dim=2), _input


if __name__ == "__main__":
    from omegaconf import OmegaConf

    hparams = OmegaConf.create(
        {
            "model": {
                "model_class_num": 3,
                "fuse_method": "add",  # can be 'concat', 'add', 'mul', 'none'
            }
        }
    )
    model = Res3DCNNATN(hparams)
    video = torch.randn(2, 3, 8, 224, 224)
    attn_map = torch.randn(2, 1, 8, 224, 224)
    output = model(video, attn_map)
    print(output.shape)
