import logging
from collections import OrderedDict

import torch
import torch.nn as nn

from utils.registry import MODEL_REGISTRY

from .base_model import BaseModel

logger = logging.getLogger("base")


@MODEL_REGISTRY.register()
class SRModel(BaseModel):
    def __init__(self, opt):
        super().__init__(opt)

        self.data_names = ["lr", "hr"]

        self.network_names = ["netSR"]
        self.networks = {}

        self.loss_names = ["sr_adv", "sr_pix", "sr_percep"]
        self.loss_weights = {}
        self.losses = {}
        self.optimizers = {}

        # define networks and load pretrained models
        nets_opt = opt["networks"]
        defined_network_names = list(nets_opt.keys())
        assert set(defined_network_names).issubset(set(self.network_names))

        for name in defined_network_names:
            setattr(self, name, self.build_network(nets_opt[name]))
            self.networks[name] = getattr(self, name)

        if self.is_train:
            train_opt = opt["train"]

            # define losses
            loss_opt = train_opt["losses"]
            defined_loss_names = list(loss_opt.keys())
            assert set(defined_loss_names).issubset(set(self.loss_names))

            for name in defined_loss_names:
                loss_conf = loss_opt.get(name)
                if loss_conf["weight"] > 0:
                    self.loss_weights[name] = loss_conf.pop("weight")
                    self.losses[name] = self.build_loss(loss_conf)

            # build optmizers
            optimizer_opt = train_opt["optimizers"]
            defined_optimizer_names = list(optimizer_opt.keys())
            assert set(defined_optimizer_names).issubset(self.networks.keys())

            for name in defined_optimizer_names:
                optim_config = optimizer_opt[name]
                self.optimizers[name] = self.build_optimizer(
                    getattr(self, name), optim_config
                )

            # set schedulers
            scheduler_opt = train_opt["scheduler"]
            self.setup_schedulers(scheduler_opt)

            # set to training state
            self.set_network_state(self.networks.keys(), "train")

    def feed_data(self, data):

        self.lr = data["src"].to(self.device)
        self.hr = data["tgt"].to(self.device)

    def forward(self):

        self.sr = self.netSR(self.lr)

    def optimize_parameters(self, step):

        self.forward()

        loss_dict = OrderedDict()

        l_sr = 0

        sr_pix = self.losses["sr_pix"](self.hr, self.sr)
        loss_dict["sr_pix"] = sr_pix
        l_sr += self.loss_weights["sr_pix"] * sr_pix

        if self.losses.get("sr_adv"):
            self.set_requires_grad(["netD"], False)
            sr_adv_g = self.calculate_rgan_loss_G(
                self.netD, self.losses["sr_adv"], self.hr, self.sr
            )
            loss_dict["sr_adv_g"] = sr_adv_g
            l_sr += self.loss_weights["sr_adv"] * sr_adv_g

        if self.losses.get("sr_percep"):
            sr_percep, sr_style = self.losses["sr_percep"](self.hr, self.sr)
            loss_dict["sr_percep"] = sr_percep
            if sr_style is not None:
                loss_dict["sr_style"] = sr_style
                l_sr += self.loss_weights["sr_percep"] * sr_style
            l_sr += self.loss_weights["sr_percep"] * sr_percep

        self.set_optimizer(names=["netSR"], operation="zero_grad")
        l_sr.backward()
        self.set_optimizer(names=["netSR"], operation="step")

        if self.losses.get("sr_adv"):
            self.set_requires_grad(["netD"], True)
            sr_adv_d = self.calculate_rgan_loss_D(
                self.netD, self.losses["sr_adv"], self.hr, self.sr
            )
            loss_dict["sr_adv_d"] = sr_adv_d

            self.optimizers["netD"].zero_grad()
            sr_adv_d.backward()
            self.optimizers["netD"].step()

        self.log_dict = self.reduce_loss_dict(loss_dict)

    def calculate_rgan_loss_D(self, netD, criterion, real, fake):

        d_pred_fake = netD(fake.detach())
        d_pred_real = netD(real)
        loss_real = criterion(
            d_pred_real - d_pred_fake.detach().mean(), True, is_disc=False
        )
        loss_fake = criterion(
            d_pred_fake - d_pred_real.detach().mean(), False, is_disc=False
        )

        loss = (loss_real + loss_fake) / 2

        return loss

    def calculate_rgan_loss_G(self, netD, criterion, real, fake):

        d_pred_fake = netD(fake)
        d_pred_real = netD(real).detach()
        loss_real = criterion(d_pred_real - d_pred_fake.mean(), False, is_disc=False)
        loss_fake = criterion(d_pred_fake - d_pred_real.mean(), True, is_disc=False)

        loss = (loss_real + loss_fake) / 2

        return loss

    def test(self, data):
        self.real_lr = data["src"].to(self.device)
        self.netSR.eval()
        with torch.no_grad():
            self.fake_real_hr = self.netSR(self.real_lr)
        self.netSR.train()

    def get_current_visuals(self, need_GT=True):
        out_dict = OrderedDict()
        out_dict["lr"] = self.real_lr.detach()[0].float().cpu()
        out_dict["sr"] = self.fake_real_hr.detach()[0].float().cpu()
        return out_dict
