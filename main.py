#!/usr/bin/env python3

import random
import argparse
import numpy
import torch
import pytorch_lightning as pl
import torchvision
import torchmetrics


def balance_classes(subset: torch.utils.data.Subset, balance: list):
	class_indices = [
		[index for index in subset.indices if subset.dataset.targets[index] == c]
		for c, _ in enumerate(subset.dataset.classes)
	]
	ref = min(len(indices) / balance[c] for c, indices in enumerate(class_indices))
	balanced_indices = [random.sample(indices, int(ref * balance[c])) for c, indices in enumerate(class_indices)]
	subset.indices = sum(balanced_indices, [])


def label_indices(datamodule: pl.LightningDataModule, indices: list):
	datamodule.data_train.indices += indices
	datamodule.data_unlabeled.indices = [index for index in datamodule.data_unlabeled.indices if index not in indices]


def label_randomly(datamodule: pl.LightningDataModule, amount: int):
	chosen_indices = random.sample(datamodule.data_unlabeled.indices, amount)
	label_indices(datamodule, chosen_indices)


def label_uncertain(datamodule: pl.LightningDataModule, amount: int, model: pl.LightningModule):
	uncertainty_list = []
	with torch.no_grad():
		for batch in datamodule.unlabeled_dataloader():
			x, _ = batch
			y_hat = model(x)
			preds = torch.nn.functional.softmax(y_hat, 1)
			uncertainty_list.append(-(preds * preds.log()).sum(1))

	uncertainty = torch.cat(uncertainty_list)
	top_uncertainties, top_indices = uncertainty.topk(amount)
	chosen_indices = [datamodule.data_unlabeled.indices[i] for i in top_indices]
	label_indices(datamodule, chosen_indices)



class MNISTDataModule(pl.LightningDataModule):
	def __init__(self, **kwargs):
		super().__init__()

		self.save_hyperparameters()
		self.transform = torchvision.transforms.ToTensor()


	def prepare_data(self):
		torchvision.datasets.MNIST(self.hparams.data_dir, train=True, download=True)
		torchvision.datasets.MNIST(self.hparams.data_dir, train=False, download=True)


	def setup(self, stage:str=None):
		if stage == "fit" or stage == "validate" or stage is None:
			data_full = torchvision.datasets.MNIST(
				self.hparams.data_dir,
				train=True,
				transform=self.transform
			)

			# Split off validation set
			self.data_unlabeled, self.data_val = torch.utils.data.random_split(
				data_full,
				[50000, 10000]
			)
			balance_classes(self.data_unlabeled, self.hparams.class_balance)
			self.data_train = torch.utils.data.Subset(data_full, [])
			label_randomly(self, self.hparams.initial_labels)

		if stage == "test" or stage is None:
			self.data_test = torchvision.datasets.MNIST(
				self.hparams.data_dir,
				train=False,
				transform=self.transform
			)


	def train_dataloader(self):
		return torch.utils.data.DataLoader(
			self.data_train,
			batch_size=self.hparams.train_batch_size,
			shuffle=True,
			num_workers=4
		)

	def val_dataloader(self):
		return torch.utils.data.DataLoader(
			self.data_val,
			batch_size=self.hparams.eval_batch_size,
			num_workers=4
		)

	def test_dataloader(self):
		return torch.utils.data.DataLoader(
			self.data_test,
			batch_size=self.hparams.eval_batch_size,
			num_workers=4
		)

	def unlabeled_dataloader(self):
		return torch.utils.data.DataLoader(
			self.data_unlabeled,
			batch_size=self.hparams.eval_batch_size,
			num_workers=4
		)



class ALModel28(pl.LightningModule):
	def __init__(self, **kwargs):
		super().__init__()

		self.save_hyperparameters()

		self.accuracy = torchmetrics.Accuracy()

		self.classifier = torch.nn.Sequential(
			torch.nn.Conv2d(1, 6, 3), torch.nn.ReLU(), torch.nn.MaxPool2d(2, 2),
			torch.nn.Conv2d(6, 16, 3), torch.nn.ReLU(), torch.nn.MaxPool2d(2, 2),
			torch.nn.Flatten(1),
			torch.nn.Linear(16*5*5, 128), torch.nn.ReLU(),
			torch.nn.Linear(128, 64), torch.nn.ReLU(),
			torch.nn.Linear(64, 10)
		)


	def forward(self, x):
		preds = self.classifier(x)

		return preds


	def training_step(self, batch, batch_idx):
		x, y = batch
		y_hat = self(x)

		loss = torch.nn.functional.cross_entropy(y_hat, y)
		self.log("training loss", loss)

		return loss

	def on_train_end(self):
		# https://github.com/PyTorchLightning/pytorch-lightning/issues/5007
		self.trainer.fit_loop.current_epoch += 1

		# To force skip early stopping the next epoch
		self.trainer.fit_loop.min_epochs = self.trainer.fit_loop.current_epoch + 1


	def validation_step(self, batch, batch_idx):
		x, y = batch
		y_hat = self(x)

		loss = torch.nn.functional.cross_entropy(y_hat, y)
		self.log("validation loss", loss)

		accuracy = self.accuracy(y_hat, y)
		self.log("validation accuracy", accuracy)

		num_labeled = float(len(self.trainer.datamodule.data_train.indices))
		self.log("labeled data", num_labeled)

		return loss


	def test_step(self, batch, batch_idx):
		x, y = batch
		y_hat = self(x)

		loss = torch.nn.functional.cross_entropy(y_hat, y)
		self.log("test loss", loss)

		accuracy = self.accuracy(y_hat, y)
		self.log("test accuracy", accuracy)

		return loss


	def configure_optimizers(self):
		return torch.optim.Adam(self.parameters(), lr=self.hparams.learning_rate)



def main():
	parser = argparse.ArgumentParser(
		formatter_class=argparse.ArgumentDefaultsHelpFormatter
	)

	# Model related
	parser.add_argument(
		'--learning-rate', type=float, default=1e-4,
		help="Multiplier used to tweak model parameters"
	)
	parser.add_argument(
		'--train-batch-size', type=int, default=16,
		help="Batch size used for training the model"
	)

	# Active learning related
	parser.add_argument(
		'--early-stopping-patience', type=int, default=5,
		help="Epochs to wait before stopping training and asking for new data"
	)
	parser.add_argument( # This should probably be made dataset-independant
		'--class-balance', type=list, default=[0.1]*5 + [1.0]*5,
		help="List of class balance multipliers"
	)
	parser.add_argument(
		'--aquisition-method', type=str, default='random',
		choices=['random', 'uncertain'],
		help="The unlabeled data aquisition method to use"
	)
	parser.add_argument(
		'--initial-labels', type=int, default=500,
		help="The amount of initially labeled datapoints"
	)
	parser.add_argument(
		'--aquisition-labels', type=int, default=100,
		help="The amount of datapoints to be labeled per aquisition step"
	)

	# Device related
	parser.add_argument(
		'--data-dir', type=str, default='./datasets',
		help="Multiplier used to tweak model parameters"
	)
	parser.add_argument(
		'--eval-batch-size', type=int, default=256,
		help="Batch size used for evaluating the model"
	)

	args = parser.parse_args()

	early_stopping_callback = pl.callbacks.early_stopping.EarlyStopping(
		monitor="validation loss",
		patience=args.early_stopping_patience
	)
	trainer = pl.Trainer(
		log_every_n_steps=10,
		max_epochs=-1,
		callbacks=[early_stopping_callback]
	)
	model = ALModel28(**vars(args))
	mnist = MNISTDataModule(**vars(args))

	for _ in range(20):
		trainer.fit(model, mnist)
		trainer.test(model, mnist)

		# TODO Could this be moved to on_train_end?
		early_stopping_callback.best_score = torch.tensor(numpy.Inf)

		# TODO Would it be possible to do this in a callback?
		if args.aquisition_method == 'random':
			label_randomly(mnist, args.aquisition_labels)
		elif args.aquisition_method == 'uncertain':
			label_uncertain(mnist, args.aquisition_labels, model)
		else:
			raise ValueError('Given aquisition method is not available')


if __name__ == "__main__":
	main()
