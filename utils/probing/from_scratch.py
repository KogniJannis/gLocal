from typing import Any, Dict, List, Tuple

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn.functional as F

from .triplet_loss import TripletLoss

Tensor = torch.Tensor


class FromScratch(pl.LightningModule):
    def __init__(
        self,
        optim_cfg: Dict[str, Any],
        model_cfg: Dict[str, str],
        extractor: Any,
    ):
        super().__init__()
        self.optim = optim_cfg["optim"]
        self.lr = optim_cfg["lr"]  # learning rate
        self.lmbda = optim_cfg["lmbda"]  # strength of regularization
        self.alpha = optim_cfg[
            "alpha"
        ]  # contribution of supervized loss to overall loss
        self.classification_batch_size = optim_cfg["classification_batch_size"]
        self.triplet_batch_size = optim_cfg["triplet_batch_size"]
        self.max_epochs = optim_cfg["max_epochs"]
        self.module = model_cfg["module"]
        self.model_name = model_cfg["model"]
        self.model = extractor.model

        self.similarity_loss_fun = TripletLoss(temperature=1.0)
        self.classification_loss_fun = torch.nn.CrossEntropyLoss()

        # Attach the hook to a named module s.t. we can access it for the similarity loss
        self.activations = None
        for name, module in self.model.named_modules():
            if name == self.module:
                module.register_forward_hook(self.save_activation())
                break

    def save_activation(self):
        def hook(model, input, output):
            self.activations = output

        return hook

    def forward(self, things_batch: Tensor, imagenet_batch_images: Tensor) -> Tensor:
        self.model(things_batch)
        things_ebmbeddings = self.activations
        imagenet_logits = self.model(imagenet_batch_images)
        return things_ebmbeddings, imagenet_logits

    @staticmethod
    def convert_predictions(sim_predictions: Tensor) -> Tensor:
        """Convert similarity predictions into odd-one-out predictions."""
        first_conversion = torch.where(
            sim_predictions != 1, sim_predictions - 2, sim_predictions
        )
        ooo_predictions = torch.where(first_conversion < 0, 2, first_conversion)
        return ooo_predictions

    @staticmethod
    def compute_similarities(
        anchor: Tensor,
        positive: Tensor,
        negative: Tensor,
    ) -> Tuple[Tensor, Tensor, Tensor]:
        """Apply the similarity function (modeled as a dot product) to each pair in the triplet."""
        sim_i = torch.sum(anchor * positive, dim=1)
        sim_j = torch.sum(anchor * negative, dim=1)
        sim_k = torch.sum(positive * negative, dim=1)
        return (sim_i, sim_j, sim_k)

    @staticmethod
    def break_ties(probas: Tensor) -> Tensor:
        # TODO: move static methods to helpers.py?
        return torch.tensor(
            [
                -1
                if (
                    torch.unique(pmf).shape[0] != pmf.shape[0]
                    or torch.unique(pmf.round(decimals=2)).shape[0] == 1
                )
                else torch.argmax(pmf)
                for pmf in probas
            ]
        )

    def accuracy_(self, probas: Tensor, batching: bool = True) -> Tensor:
        choices = self.break_ties(probas)
        argmax = np.where(choices == 0, 1, 0)
        acc = argmax.mean() if batching else argmax.tolist()
        return acc

    def choice_accuracy(self, similarities: float) -> float:
        probas = F.softmax(torch.stack(similarities, dim=-1), dim=1)
        choice_acc = self.accuracy_(probas)
        return choice_acc

    def classification_accuracy(self, logits: Tensor, labels: Tensor) -> float:
        max_idcs = logits.argmax(dim=1)
        acc = (max_idcs == labels).sum().item() / len(labels)
        return acc

    @staticmethod
    def unbind(embeddings: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
        return torch.unbind(
            torch.reshape(embeddings, (3, -1, *embeddings.shape[1:])),
        )

    def _step(self, batch: Tuple[Tensor, Tensor], batch_idx: int):
        # Run data through model
        things_batch, imagenet_batch = batch
        things_batch_images = torch.cat([things_batch[0], things_batch[1], things_batch[2]], dim=0) # should be [bs*3 x 3 x w x h]
        imagenet_batch_images, imagenet_batch_labels = imagenet_batch
        things_embeddings, imagenet_logits = self(things_batch_images, imagenet_batch_images)
        # Calculate similarity loss
        anchor, positive, negative = self.unbind(things_embeddings) # should be [bs*3 x 3 x w x h] -> 3 x [bs x 3 x w x h]
        dots = self.compute_similarities(anchor, positive, negative)
        similarity_loss = self.similarity_loss_fun(dots)
        similarity_acc = self.choice_accuracy(dots)
        # Calculate classification loss
        classification_loss = self.classification_loss_fun(imagenet_logits, imagenet_batch_labels)
        classification_acc = self.classification_accuracy(imagenet_logits, imagenet_batch_labels)
        # Combine & log losses
        loss = (1 - self.alpha) * similarity_loss + self.alpha * classification_loss
        return (
            loss,
            classification_loss,
            classification_acc,
            similarity_loss,
            similarity_acc,
        )

    def training_step(self, batch: Tuple[Tensor, Tensor], batch_idx: int):
        (
            loss,
            classification_loss,
            classification_acc,
            similarity_loss,
            similarity_acc,
        ) = self._step(batch, batch_idx)
        self.log("train_loss", loss, on_epoch=True)
        self.log("train_imgnt_loss", classification_loss, on_epoch=True)
        self.log("train_imgnt_acc", classification_acc, on_epoch=True)
        self.log("train_things_loss", similarity_loss, on_epoch=True)
        self.log("train_things_acc", similarity_acc, on_epoch=True)
        return loss

    def validation_step(self, batch: Tuple[Tensor, Tensor], batch_idx: int):
        (
            loss,
            classification_loss,
            classification_acc,
            similarity_loss,
            similarity_acc,
        ) = self._step(batch, batch_idx)
        metrics = {
            "val_loss": loss,
            "val_imgnt_loss": classification_loss,
            "val_imgnt_acc": classification_acc,
            "val_things_loss": similarity_loss,
            "val_things_acc": similarity_acc,
        }
        self.log_dict(metrics)
        return metrics

    def test_step(self, batch: Tuple[Tensor, Tensor], batch_idx: int):
        (
            loss,
            classification_loss,
            classification_acc,
            similarity_loss,
            similarity_acc,
        ) = self._step(batch, batch_idx)
        metrics = {
            "test_loss": loss,
            "test_imgnt_loss": classification_loss,
            "test_imgnt_acc": classification_acc,
            "test_things_loss": similarity_loss,
            "test_things_acc": similarity_acc,
        }
        self.log_dict(metrics)
        return metrics

    def predict_step(self, batch: Tuple[Tensor, Tensor], batch_idx: int):
        # TODO: predict imagenet, combine with "self._step"
        # Run data through model
        things_batch, imagenet_batch = batch
        things_batch_images = torch.cat([things_batch[0], things_batch[1], things_batch[2]],
                                        dim=0)  # should be [bs*3 x 3 x w x h]
        imagenet_batch_images, imagenet_batch_labels = imagenet_batch
        things_embeddings, imagenet_logits = self(things_batch_images, imagenet_batch_images)
        # Calculate similarity loss
        anchor, positive, negative = self.unbind(things_embeddings)  # should be [bs*3 x 3 x w x h] -> 3 x [bs x 3 x w x h]
        similarities = self.compute_similarities(anchor, positive, negative)

        sim_predictions = torch.argmax(
            F.softmax(torch.stack(similarities, dim=1), dim=1), dim=1
        )
        ooo_predictions = self.convert_predictions(sim_predictions)
        return ooo_predictions

        raise NotImplementedError("This method is not implemented yet.")

    def backward(self, loss, optimizer, optimizer_idx):
        loss.backward()

    def configure_optimizers(self):
        if self.optim.lower() == "adam":
            optimizer = getattr(torch.optim, self.optim.capitalize())
            optimizer = optimizer(
                self.model.parameters(), lr=self.lr, weight_decay=self.lmbda
            )
        elif self.optim.lower() == "sgd":
            optimizer = getattr(torch.optim, self.optim.upper())
            optimizer = optimizer(
                self.model.parameters(),
                lr=self.lr,
                momentum=0.9,
                weight_decay=self.lmbda,
            )
        else:
            raise ValueError(
                "\nUse Adam or SGD for learning a linear transformation of a network's feature space.\n"
            )
        return optimizer