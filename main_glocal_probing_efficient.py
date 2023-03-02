import argparse
import os
import pickle
import warnings
from typing import Any, Callable, Dict, Iterator, List, Tuple

import numpy as np
import pandas as pd
import torch
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from sklearn.model_selection import KFold
from torch.utils.data import DataLoader
from tqdm import tqdm

import utils

Array = np.ndarray
Tensor = torch.Tensor
FrozenDict = Any


def parseargs():
    parser = argparse.ArgumentParser()

    def aa(*args, **kwargs):
        parser.add_argument(*args, **kwargs)

    aa("--data_root", type=str, help="path/to/things")
    aa("--imagenet_features_root", type=str, help="path/to/imagenet/features")
    aa("--dataset", type=str, help="Which dataset to use", default="things")
    aa("--model", type=str)
    aa(
        "--module",
        type=str,
        default="penultimate",
        help="neural network module for which to learn a linear transform",
        choices=["penultimate", "logits"],
    )
    aa(
        "--source",
        type=str,
        default="torchvision",
        choices=[
            "google",
            "loss",
            "custom",
            "ssl",
            "imagenet",
            "torchvision",
            "vit_same",
            "vit_best",
        ],
    )
    aa(
        "--n_objects",
        type=int,
        help="Number of object categories in the data",
        default=1854,
    )
    aa("--optim", type=str, default="Adam", choices=["Adam", "AdamW", "SGD"])
    aa("--learning_rate", type=float, metavar="eta", default=1e-3)
    aa("--regularization", type=str, default="l2", choices=["l2", "eye"])
    aa(
        "--alpha",
        type=float,
        default=1e-1,
        help="Relative contribution of the contrastive loss term",
        choices=[
            5e-1,
            4e-1,
            3e-1,
            2e-1,
            1e-1,
            5e-2,
            5e-2,
            4e-2,
            3e-2,
            2e-2,
            1e-2,
            1e-2,
        ],
    )
    aa(
        "--tau",
        type=float,
        default=1,
        help="temperature value for contrastive learning objective",
    )
    aa(
        "--lmbda",
        type=float,
        default=1e-3,
        help="Relative contribution of the l2 or identity regularization term",
        choices=[1e-1, 1e-2, 1e-3, 1e-4, 1e-5],
    )
    aa(
        "--sigma",
        type=float,
        default=1e-3,
        help="Scalar to scale a neural net's pre-transformed representation space prior to the optimization process",
        choices=[1e-1, 1e-2, 1e-3, 1e-4],
    )
    aa(
        "--triplet_batch_size",
        type=int,
        default=256,
        help="Use power of 2 for running optimization process on GPU",
        choices=[64, 128, 256, 512, 1024],
    )
    aa(
        "--contrastive_batch_size",
        type=int,
        default=1024,
        help="Use power of 2 for running optimization process on GPU",
        choices=[128, 256, 512, 1024, 2048, 4096],
    )
    aa(
        "--epochs",
        type=int,
        help="Maximum number of epochs to perform finetuning",
        default=100,
    )
    aa(
        "--burnin",
        type=int,
        help="Minimum number of epochs to perform finetuning",
        default=10,
    )
    aa(
        "--patience",
        type=int,
        help="number of checks with no improvement after which training will be stopped",
        default=10,
    )
    aa("--device", type=str, default="cpu", choices=["cpu", "gpu"])
    aa(
        "--features_type",
        type=str,
        default="hdf5",
        help="In which data format ImageNet features have been saved to disk",
        choices=["hdf5", "pt"],
    )
    aa(
        "--num_processes",
        type=int,
        default=4,
        choices=[2, 4, 6, 8, 10, 12],
        help="Number of devices to use for performing distributed training on CPU",
    )
    aa(
        "--use_bias",
        action="store_true",
        help="whether to use a bias in the linear probe",
    )
    aa("--probing_root", type=str, help="path/to/probing")
    aa("--log_dir", type=str, help="directory to checkpoint transformations")
    aa("--rnd_seed", type=int, default=42, help="random seed for reproducibility")
    args = parser.parse_args()
    return args


def create_optimization_config(args) -> Dict[str, Any]:
    """Create frozen config dict for optimization hyperparameters."""
    optim_cfg = dict()
    optim_cfg["optim"] = args.optim
    optim_cfg["lr"] = args.learning_rate
    optim_cfg["reg"] = args.regularization
    optim_cfg["lmbda"] = args.lmbda
    optim_cfg["alpha"] = args.alpha
    optim_cfg["tau"] = args.tau
    optim_cfg["contrastive_batch_size"] = args.contrastive_batch_size
    optim_cfg["triplet_batch_size"] = args.triplet_batch_size
    optim_cfg["max_epochs"] = args.epochs
    optim_cfg["min_epochs"] = args.burnin
    optim_cfg["patience"] = args.patience
    optim_cfg["use_bias"] = args.use_bias
    optim_cfg["ckptdir"] = os.path.join(args.log_dir, args.model, args.module)
    optim_cfg["sigma"] = args.sigma
    return optim_cfg


def load_features(probing_root: str, subfolder: str = "embeddings") -> Dict[str, Array]:
    """Load features for THINGS objects from disk."""
    with open(os.path.join(probing_root, subfolder, "features.pkl"), "rb") as f:
        features = pickle.load(f)
    return features


def get_temperature(
    model_config, model: List[str], module: str, objective: str = "cosine"
) -> List[str]:
    """Get optimal temperature values for all models."""
    try:
        temp = model_config[model][module]["temperature"][objective]
    except KeyError:
        temp = 1.0
        warnings.warn(
            f"\nMissing temperature value for {model} and {module} layer.\nSetting temperature value to 1.\n"
        )
    return temp


def get_batches(
    dataset: Tensor, batch_size: int, train: bool, num_workers: int = 0
) -> Iterator:
    batches = DataLoader(
        dataset=dataset,
        batch_size=batch_size,
        shuffle=True if train else False,
        num_workers=num_workers,
        drop_last=False,
        pin_memory=True if train else False,
    )
    return batches


def get_callbacks(optim_cfg: FrozenDict, steps: int = 20) -> List[Callable]:
    if not os.path.exists(optim_cfg["ckptdir"]):
        os.makedirs(optim_cfg["ckptdir"])
        print("\nCreating directory for checkpointing...\n")
    checkpoint_callback = ModelCheckpoint(
        monitor="val_loss",
        dirpath=optim_cfg["ckptdir"],
        filename="ooo-finetuning-epoch{epoch:02d}-val_loss{val/loss:.2f}",
        auto_insert_metric_name=False,
        every_n_epochs=steps,
    )
    early_stopping = EarlyStopping(
        monitor="val_loss",
        min_delta=1e-4,
        mode="min",
        patience=optim_cfg["patience"],
        verbose=True,
        check_finite=True,
    )
    callbacks = [checkpoint_callback, early_stopping]
    return callbacks


def get_mean_cv_acc(
    cv_results: Dict[str, List[float]], metric: str = "test_acc"
) -> float:
    avg_val_acc = np.mean([vals[0][metric] for vals in cv_results.values()])
    return avg_val_acc


def get_mean_cv_loss(
    cv_results: Dict[str, List[float]], metric: str = "test_loss"
) -> float:
    avg_val_loss = np.mean([vals[0][metric] for vals in cv_results.values()])
    return avg_val_loss


def make_results_df(
    columns: List[str],
    probing_acc: float,
    probing_loss: float,
    ooo_choices: Array,
    model_name: str,
    module_name: str,
    source: str,
    reg: str,
    optim: str,
    lr: float,
    alpha: float,
    lmbda: float,
    tau: float,
    bias: bool,
) -> pd.DataFrame:
    probing_results_current_run = pd.DataFrame(index=range(1), columns=columns)
    probing_results_current_run["model"] = model_name
    probing_results_current_run["probing"] = probing_acc
    probing_results_current_run["cross-entropy"] = probing_loss
    # probing_results_current_run["choices"] = [ooo_choices]
    probing_results_current_run["module"] = module_name
    probing_results_current_run["family"] = utils.analyses.get_family_name(model_name)
    probing_results_current_run["source"] = source
    probing_results_current_run["reg"] = reg
    probing_results_current_run["alpha"] = alpha
    probing_results_current_run["lmbda"] = lmbda
    probing_results_current_run["tau"] = tau
    probing_results_current_run["optim"] = optim.lower()
    probing_results_current_run["lr"] = lr
    probing_results_current_run["contrastive"] = True
    probing_results_current_run["bias"] = bias
    return probing_results_current_run


def save_results(
    args, probing_acc: float, probing_loss: float, ooo_choices: Array
) -> None:
    out_path = os.path.join(args.probing_root, "results")
    if not os.path.exists(out_path):
        print("\nCreating results directory...\n")
        os.makedirs(out_path)

    if os.path.isfile(os.path.join(out_path, "probing_results.pkl")):
        print(
            "\nFile for probing results exists.\nConcatenating current results with existing results file...\n"
        )
        probing_results_overall = pd.read_pickle(
            os.path.join(out_path, "probing_results.pkl")
        )
        probing_results_current_run = make_results_df(
            columns=probing_results_overall.columns.values,
            probing_acc=probing_acc,
            probing_loss=probing_loss,
            ooo_choices=ooo_choices,
            model_name=args.model,
            module_name=args.module,
            source=args.source,
            reg=args.regularization,
            optim=args.optim,
            lr=args.learning_rate,
            alpha=args.alpha,
            lmbda=args.lmbda,
            tau=args.tau,
            bias=args.use_bias,
        )
        probing_results = pd.concat(
            [probing_results_overall, probing_results_current_run],
            axis=0,
            ignore_index=True,
        )
        probing_results.to_pickle(os.path.join(out_path, "probing_results.pkl"))
    else:
        print("\nCreating file for probing results...\n")
        columns = [
            "model",
            "probing",
            "cross-entropy",
            # "choices",
            "module",
            "family",
            "source",
            "reg",
            "optim",
            "lr",
            "alpha",
            "lambda",
            "tau",
            "bias",
            "contrastive",
        ]
        probing_results = make_results_df(
            columns=columns,
            probing_acc=probing_acc,
            probing_loss=probing_loss,
            ooo_choices=ooo_choices,
            model_name=args.model,
            module_name=args.module,
            source=args.source,
            reg=args.regularization,
            alpha=args.alpha,
            lmbda=args.lmbda,
            tau=args.tau,
            optim=args.optim,
            lr=args.learning_rate,
            bias=args.use_bias,
        )
        probing_results.to_pickle(os.path.join(out_path, "probing_results.pkl"))


def run(
    features: Array,
    imagenet_features_root: str,
    data_root: str,
    optim_cfg: Dict[str, Any],
    n_objects: int,
    device: str,
    rnd_seed: int,
    num_processes: int,
    features_type: str,
) -> Tuple[Dict[str, List[float]], Array]:
    """Run optimization process."""
    callbacks = get_callbacks(optim_cfg)
    if features_type == "hdf":
        imagenet_train_features = utils.probing.FeaturesHDF5(
            root=imagenet_features_root,
            split="train_set",
            device=device,
        )
        imagenet_val_features = utils.probing.FeaturesHDF5(
            root=imagenet_features_root,
            split="val",
            device=device,
        )
    elif features_type == "pt":
        imagenet_train_features = utils.probing.FeaturesPT(
            root=imagenet_features_root,
            split="train_set",
            device=device,
        )
        imagenet_val_features = utils.probing.FeaturesPT(
            root=imagenet_features_root,
            split="val",
            device=device,
        )
    else:
        raise ValueError(
            "\nCan only create dataset for features that were saved in either 'pt' or 'hdf5' format.\n"
        )
    triplets = utils.probing.load_triplets(data_root)
    features = (
        features - features.mean()
    ) / features.std()  # subtract global mean and normalize by standard deviation of feature matrix
    objects = np.arange(n_objects)
    # For glocal optimization, we don't need to perform k-Fold cross-validation (we can simply set k=4 or 5)
    kf = KFold(n_splits=4, random_state=rnd_seed, shuffle=True)
    cv_results = {}
    ooo_choices = []
    for k, (train_idx, _) in tqdm(enumerate(kf.split(objects), start=1), desc="Fold"):
        train_objects = objects[train_idx]
        # partition triplets into disjoint object sets
        triplet_partitioning = utils.probing.partition_triplets(
            triplets=triplets,
            train_objects=train_objects,
        )
        train_triplets = utils.probing.TripletData(
            triplets=triplet_partitioning["train"],
            n_objects=n_objects,
        )
        val_triplets = utils.probing.TripletData(
            triplets=triplet_partitioning["val"],
            n_objects=n_objects,
        )
        train_batches_things = get_batches(
            dataset=train_triplets,
            batch_size=optim_cfg["triplet_batch_size"],
            train=True,
            num_workers=0,
        )
        train_batches_imagenet = get_batches(
            dataset=imagenet_train_features,
            batch_size=optim_cfg["contrastive_batch_size"],
            train=True,
            num_workers=num_processes,
        )
        val_batches_things = get_batches(
            dataset=val_triplets,
            batch_size=optim_cfg["triplet_batch_size"],
            train=False,
        )
        val_batches_imagenet = get_batches(
            dataset=imagenet_val_features,
            batch_size=optim_cfg["contrastive_batch_size"],
            train=True,
            num_workers=num_processes,
        )
        train_batches = utils.probing.zip_batches(
            train_batches_things, train_batches_imagenet
        )
        val_batches = utils.probing.zip_batches(
            val_batches_things, val_batches_imagenet
        )
        glocal_probe = utils.probing.GlocalFeatureProbe(
            features=features,
            optim_cfg=optim_cfg,
        )
        trainer = Trainer(
            accelerator=device,
            callbacks=callbacks,
            # strategy="ddp_spawn" if device == "cpu" else None,
            strategy="ddp",
            max_epochs=optim_cfg["max_epochs"],
            min_epochs=optim_cfg["min_epochs"],
            devices=num_processes if device == "cpu" else "auto",
            enable_progress_bar=True,
            gradient_clip_val=1.0,
            gradient_clip_algorithm="norm",
        )
        trainer.fit(glocal_probe, train_batches, val_batches)
        val_performance = trainer.test(
            glocal_probe,
            dataloaders=val_batches,
        )
        predictions = trainer.predict(glocal_probe, dataloaders=val_batches_things)
        predictions = torch.cat(predictions, dim=0).tolist()
        ooo_choices.append(predictions)
        cv_results[f"fold_{k:02d}"] = val_performance
        break
    transformation = {}
    transformation["weights"] = glocal_probe.transform_w.data.detach().cpu().numpy()
    if optim_cfg["use_bias"]:
        transformation["bias"] = glocal_probe.transform_b.data.detach().cpu().numpy()
    ooo_choices = np.concatenate(ooo_choices)
    return ooo_choices, cv_results, transformation


if __name__ == "__main__":
    # parse arguments
    args = parseargs()
    # seed everything for reproducibility of results
    seed_everything(args.rnd_seed, workers=True)
    features = load_features(args.probing_root)
    model_features = features[args.source][args.model][args.module]
    optim_cfg = create_optimization_config(args)
    ooo_choices, cv_results, transform = run(
        features=model_features,
        imagenet_features_root=args.imagenet_features_root,
        data_root=args.data_root,
        optim_cfg=optim_cfg,
        n_objects=args.n_objects,
        device=args.device,
        rnd_seed=args.rnd_seed,
        num_processes=args.num_processes,
        features_type=args.features_type,
    )
    avg_cv_acc = get_mean_cv_acc(cv_results)
    avg_cv_loss = get_mean_cv_loss(cv_results)
    save_results(
        args, probing_acc=avg_cv_acc, probing_loss=avg_cv_loss, ooo_choices=ooo_choices
    )

    out_path = os.path.join(
        args.probing_root,
        "results",
        args.source,
        args.model,
        args.module,
        str(args.alpha),
        str(args.lmbda),
        str(args.tau),
        args.optim.lower(),
        str(args.learning_rate),
    )
    if not os.path.exists(out_path):
        os.makedirs(out_path, exist_ok=True)

    if optim_cfg["use_bias"]:
        with open(os.path.join(out_path, "transform.npz"), "wb") as f:
            np.savez_compressed(
                file=f, weights=transform["weights"], bias=transform["bias"]
            )
    else:
        with open(os.path.join(out_path, "transform.npz"), "wb") as f:
            np.savez_compressed(file=f, weights=transform["weights"])
