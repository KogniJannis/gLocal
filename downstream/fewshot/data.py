import os
import torch
import numpy as np
from typing import Optional
from torchvision.datasets import CIFAR100, DTD, SUN397

Array = np.ndarray
Tensor = torch.Tensor


def load_dataset(
    name: str,
    data_dir: str,
    train: bool,
    transform=None,
    embeddings: Optional[Array] = None,
):
    if name == "cifar100":
        dataset_class = CIFAR100
        if embeddings is not None:
            dataset_class = embed_dataset(dataset_class, embeddings)
        dataset = dataset_class(
            root=data_dir,
            train=train,
            download=True,
            transform=transform,
        )
    elif name == "DTD":
        dataset_class = DTD
        if embeddings is not None:
            dataset_class = embed_dataset(dataset_class, embeddings)
        dataset = dataset_class(
            root=data_dir,
            split="train" if train else "test",
            download=True,
            transform=transform,
        )
    elif name == "SUN397":
        dataset_class = SUN397
        if embeddings is not None:
            dataset_class = embed_dataset(dataset_class, embeddings)
        dataset = dataset_class(
            root=data_dir,
            download=True,
            transform=transform,
        )
        if train:
            split_file = "Training_01.txt"
        else:
            split_file = "Testing_01.txt"
        with open(os.path.join(dataset.root, split_file)) as f:
            lines = f.read()
        file_names = [l for l in lines.split("\n") if not l == ""]
        dataset._image_files = [os.path.join(dataset._data_dir, fn[1:]) for fn in file_names]
        dataset._labels = [
            dataset.class_to_idx["/".join(path.split("/")[2:-1])] for path in file_names
        ]
    else:
        raise ValueError("\nUnknown dataset\n")

    return dataset


def embed_dataset(dataset, embeddings):
    """Wraps a dataset such that it ueses the given embeddings as features."""

    def __getitem__(self, idx):
        if hasattr(self, "targets"):
            label = self.targets[idx]
        else:
            label = self._labels[idx]

        if hasattr(self, "_image_files"):
            embedding = embeddings[str(self._image_files[idx])]
        else:
            embedding = embeddings[idx]

        if self.target_transform:
            label = self.target_transform(label)
        return embedding, label

    return type(
        dataset.__name__,
        (dataset,),
        {
            "__getitem__": __getitem__,
        },
    )
