"""
Training Script for UNet-based Cahn-Hilliard Equation Solver

This module provides functionality for training a UNet surrogate model
for the Cahn-Hilliard equation. It includes training, validation, model
checkpointing functionalities, and logs key training metrics.
"""

import argparse
from datetime import datetime
import logging
import os
import random

import numpy as np
import torch
from torch import optim
from torch.utils.data import DataLoader
from torch.nn import MSELoss

from pipeline.dataset.loaders import H5Dataset
from pipeline.model.model import UNet2d


def setup_directories(timestring: str, args) -> tuple[str, str, str]:
    """
    Creates necessary directories for saving model and logs.

    Args:
        timestring (str): Unique timestamp used for naming files and directories.
        args (argparse.Namespace): Parsed arguments containing batch_size and time_skip.

    Returns:
        str: Path to the final model save location.
    """
    model_path = f'model_{timestring}'
    log_path = f'{model_path}/log'
    save_path = (
        f'{model_path}/model_savetime_{timestring}'
        f'_batchsize_{args.batch_size}_timeskip_{args.time_skip}.pt'
    )

    os.makedirs(model_path, exist_ok=True)
    os.makedirs(log_path, exist_ok=True)

    return save_path, model_path, log_path


def configure_logging(path: str) -> None:
    """
    Sets up the logging configuration to log training and validation details.

    This function configures logging to both console and a file.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(f'{path}/train.log', mode='w', encoding='utf-8')
        ]
    )


def create_model(device: torch.device) -> torch.nn.Module:
    """
    Initializes the UNet model with predefined input and output channels.

    Args:
        device (torch.device): The device to move the model to ('cpu' or 'cuda').

    Returns:
        torch.nn.Module: The initialized UNet model.
    """
    in_channels = 1
    out_channels = in_channels
    features = 16

    model = UNet2d(
        in_channels=in_channels,
        out_channels=out_channels,
        features=features,
    )

    model.to(device)

    return model


def calculate_parameters(model: torch.nn.Module) -> int:
    """
    Calculates the total number of trainable parameters in the model.

    Args:
        model (torch.nn.Module): The UNet model.

    Returns:
        int: Total number of trainable parameters.
    """
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    n_params = sum([np.prod(p.size()) for p in model_parameters])

    return n_params


def main(args: argparse.Namespace) -> None:
    """
    Main function for training the UNet model.

    Args:
        args (argparse.Namespace): The parsed command-line arguments.
    """
    # Setup directories and logging
    date_time = datetime.now()
    timestring = (
        f'{date_time.month}{date_time.day}{date_time.hour}{date_time.minute}'
    )
    save_path, model_path, log_path = setup_directories(timestring, args)
    configure_logging(log_path)

    # Set device (cuda if available)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    logging.info(f"Using device: {device}")

    # Set random seed for reproducibility
    seed_val = 2023
    torch.manual_seed(seed_val)
    random.seed(seed_val)
    np.random.seed(seed_val)

    # Load datasets
    train_dataset = H5Dataset(path='data', mode='train', skip=args.time_skip)
    valid_dataset = H5Dataset(path='data', mode='valid', skip=args.time_skip)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    valid_loader = DataLoader(valid_dataset, batch_size=args.batch_size, shuffle=False)

   # Initialize model
    model = create_model(device)

    # Report the number of model parameters
    n_params = calculate_parameters(model)
    logging.info(f'Model size: {n_params} trainable parameters')

    # Define loss function and optimizer
    loss_fn = MSELoss(reduction='mean')
    optimizer = optim.AdamW(
        model.parameters(),
        lr=args.lr,
        weight_decay=args.weight_decay,
    )
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode='min',
        factor=args.lr_decay,
        patience=5,
        min_lr=1e-5,
    )

    min_val_loss = float('inf')
    
    # Helper functions for training and validation
    # In my experiments, it seems better to compile the entire training step
    # rather than just the model. This issue also is raised in:
    # https://discuss.pytorch.org/t/torch-compile-what-is-the-best-scope-of-compilation/185442

    @torch.compile
    def train_step(net: torch.nn.Module, x: torch.tensor, y: torch.tensor):
        optimizer.zero_grad()
        pred = net(x)
        loss = loss_fn(pred, y)
        loss.backward()
        optimizer.step()
        return loss

    @torch.compile
    def valid_step(net: torch.nn.Module, x: torch.tensor, y: torch.tensor):
        pred = net(x)
        loss = loss_fn(pred, y)
        return loss

    # Training loop
    for epoch in range(args.n_epochs):
        epoch_lr = optimizer.param_groups[0]['lr']
        logging.info(f'Epoch {epoch}/{args.n_epochs}, learning rate {epoch_lr}')

        # Training step
        model.train()
        for step, (xb, yb) in enumerate(train_loader):
            xb = xb.to(device)
            yb = yb.to(device)
            loss = train_step(model, xb, yb)

            # Log training loss
            if step % args.log_freq == 0:
                logging.info(f'Train Step {step}/{len(train_loader)} - Loss: {loss.item()}')

        # Validation step
        model.eval()
        if epoch % args.valid_freq == 0:
            valid_loss = []
            with torch.no_grad():
                for xb, yb in valid_loader:
                    xb = xb.to(device)
                    yb = yb.to(device)
                    loss = valid_step(model, xb, yb)
                    valid_loss.append(loss.item())

            # Log validation loss
            val_loss = np.mean(valid_loss)
            logging.info(f'Validation Loss: {val_loss}')

            # Checkpoint best model so far
            if val_loss < min_val_loss:
                # the compiled and uncompiled model share the same weights,
                # so we will save the uncompiled model.
                torch.save(model.state_dict(), f'{model_path}/checkpoint_model_tskip_{args.time_skip}.pt')
                min_val_loss = val_loss
                logging.info(f'Checkpointing a new best model...')

            # Adjust learning rate
            scheduler.step(val_loss)

    # Final model save
    torch.save(model.state_dict(), save_path)
    logging.info(f'Final model saved at {save_path}')


if __name__ == "__main__":
    # Argument parser
    parser = argparse.ArgumentParser(description='Train a UNET-based PDE solver for Cahn-Hilliard system.')

    # Training parameters
    parser.add_argument('--batch_size', type=int, default=64, help='Number of samples in each minibatch')
    parser.add_argument('--time_skip', type=int, default=25, help='Number of time steps to skip during prediction/inference')
    parser.add_argument('--n_epochs', type=int, default=10, help='Number of training epochs')
    parser.add_argument('--lr', type=float, default=1e-3, help='Learning rate')
    parser.add_argument('--lr_decay', type=float, default=0.4, help='Learning rate decay factor')
    parser.add_argument('--weight_decay', type=float, default=1e-6, help='Weight decay for optimizer')

    # Miscellaneous
    parser.add_argument('--valid_freq', type=int, default=1, help='Number of epochs between validation steps')
    parser.add_argument('--log_freq', type=int, default=1, help='Logging frequency for training steps')

    # Parse arguments and run main function
    args = parser.parse_args()
    main(args)
