import copy
import gc
import os
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import napari
import numpy as np
import torch
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg as FigureCanvas,
)
from matplotlib.figure import Figure

# MONAI
from monai.data import DataLoader
from monai.data import decollate_batch
from monai.data import pad_list_data_collate
from monai.data import PatchDataset
from monai.losses import DiceCELoss
from monai.losses import DiceFocalLoss
from monai.losses import DiceLoss
from monai.losses import FocalLoss
from monai.losses import GeneralizedDiceLoss
from monai.losses import TverskyLoss
from monai.metrics import DiceMetric
from monai.transforms import AsDiscrete
from monai.transforms import Compose
from monai.transforms import EnsureChannelFirstd
from monai.transforms import EnsureType
from monai.transforms import EnsureTyped
from monai.transforms import LoadImaged
from monai.transforms import Rand3DElasticd
from monai.transforms import RandAffined
from monai.transforms import RandFlipd
from monai.transforms import RandRotate90d
from monai.transforms import RandShiftIntensityd
from monai.transforms import RandSpatialCropSamplesd
from monai.transforms import SpatialPadd
from napari.qt.threading import thread_worker

# Qt
from qtpy.QtWidgets import QComboBox
from qtpy.QtWidgets import QLabel
from qtpy.QtWidgets import QLayout
from qtpy.QtWidgets import QPushButton
from qtpy.QtWidgets import QSizePolicy
from qtpy.QtWidgets import QSpinBox
from qtpy.QtWidgets import QVBoxLayout
from qtpy.QtWidgets import QWidget

from napari_cellseg_annotator import utils
from napari_cellseg_annotator.model_framework import ModelFramework


class Trainer(ModelFramework):
    """A plugin to train pre-defined Pytorch models for one-channel segmentation directly in napari.
    Features parameter selection for training, dynamic loss plotting and automatic saving of the best weights during
    training through validation."""

    def __init__(
        self,
        viewer: "napari.viewer.Viewer",
        data_path="",
        label_path="",
        results_path="",
        model_index=0,
        loss_index=0,
        epochs=10,
        samples=15,
        batch=1,
        val_interval=2,
    ):
        """Creates a Trainer widget with the following functionalities :

        * A filetype choice to select images in a folder

        * A button to choose the folder containing the images of the dataset. Validation files are chosen automatically from the whole dataset.

        * A button to choose the label folder (must have matching number and name of images)

        * A button to choose where to save the results (weights). Defaults to the plugin's models/saved_weights folder

        * A dropdown menu to choose which model to train

        * A dropdown menu to choose which loss function to use (see https://docs.monai.io/en/stable/losses.html)

        * A spin box to choose the number of epochs to train for

        * A spin box to choose the batch size during training

        * A spin box to choose the number of samples to take from an image when training

        TODO:

        * Choice of validation proportion, validation interval, sampling behaviour

        * Custom model loading


        Args:
            viewer: napari viewer to display the widget in

            data_path (str): path to images

            label_path (str): path to labels

            results_path (str): path to results

            model_index (int): model to select by default

            loss_index (int): loss to select by default

            epochs (uint): number of epochs

            samples (uint):  number of samples

            batch (uint): batch size

            val_interval (uint) : epoch interval for validation

        """

        super().__init__(viewer)

        # self.master = parent
        self._viewer = viewer
        """napari.viewer.Viewer: viewer in which the widget is displayed"""

        ######################
        ######################
        ######################
        # TEST TODO REMOVE
        import glob

        directory = os.path.dirname(os.path.realpath(__file__)) + str(
            Path("/models/dataset/volumes")
        )
        self.data_path = directory

        lab_directory = os.path.dirname(os.path.realpath(__file__)) + str(
            Path("/models/dataset/lab_sem")
        )
        self.label_path = lab_directory

        self.images_filepaths = sorted(
            glob.glob(os.path.join(directory, "*.tif"))
        )

        self.labels_filepaths = sorted(
            glob.glob(os.path.join(lab_directory, "*.tif"))
        )

        #######################
        #######################
        #######################
        if results_path == "":
            self.results_path = os.path.dirname(
                os.path.realpath(__file__)
            ) + str(Path("/models/saved_weights"))
        else:
            self.results_path = results_path

        if data_path != "":
            self.data_path = data_path

        if label_path != "":
            self.label_path = label_path

        # recover default values
        self.num_samples = samples
        """Number of samples to extract"""
        self.batch_size = batch

        self.epochs = epochs

        self.val_interval = val_interval

        self.model = None  # TODO : custom model loading ?
        self.worker = None
        """Training worker for multithreading"""
        self.data = None

        self.loss_dict = {
            "Dice loss": DiceLoss(sigmoid=True),
            "Focal loss": FocalLoss(),
            "Dice-Focal loss": DiceFocalLoss(sigmoid=True, lambda_dice=0.5),
            "Generalized Dice loss": GeneralizedDiceLoss(sigmoid=True),
            "DiceCELoss": DiceCELoss(sigmoid=True),
            "Tversky loss": TverskyLoss(sigmoid=True),
        }
        """Dict of loss functions"""

        self.metric_values = []
        """List of dice metric validation values"""
        self.epoch_loss_values = []
        """List of loss values per epoch"""
        self.canvas = None
        """Canvas to plot loss and dice metric in"""
        self.train_loss_plot = None
        """Plot for loss"""
        self.dice_metric_plot = None
        """Plot for dice metric"""

        self.model_choice.setCurrentIndex(model_index)

        ################################
        # interface
        self.epoch_choice = QSpinBox()
        self.epoch_choice.setValue(self.epochs)
        self.epoch_choice.setRange(2, 1000)
        # self.epoch_choice.setSingleStep(2)
        self.epoch_choice.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.lbl_epoch_choice = QLabel("Number of epochs : ", self)

        self.loss_choice = QComboBox()
        self.loss_choice.setCurrentIndex(loss_index)
        self.loss_choice.addItems(sorted(self.loss_dict.keys()))
        self.lbl_loss_choice = QLabel("Loss function", self)

        self.sample_choice = QSpinBox()
        self.sample_choice.setValue(self.num_samples)
        self.sample_choice.setRange(2, 50)
        self.sample_choice.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.lbl_sample_choice = QLabel(
            "Number of samples from image : ", self
        )

        self.batch_choice = QSpinBox()
        self.batch_choice.setValue(self.batch_size)
        self.batch_choice.setRange(1, 10)
        self.batch_choice.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.lbl_batch_choice = QLabel("Batch size : ", self)

        self.val_interval_choice = QSpinBox()
        self.val_interval_choice.setValue(self.val_interval)
        self.val_interval_choice.setRange(1, 10)
        self.val_interval_choice.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed
        )
        self.lbl_val_interv_choice = QLabel("Validation interval : ", self)

        self.btn_start = QPushButton("Start training")
        self.btn_start.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_start.clicked.connect(self.start)

        self.btn_model_path.setVisible(False)
        self.lbl_model_path.setVisible(False)

        self.build()

    def check_ready(self):
        """
        Checks that the paths to the images and labels are correctly set

        Returns:

            * True if paths are set correctly (!=[])

            * False and displays a warning if not

        """
        if self.images_filepaths != [""] and self.labels_filepaths != [""]:
            return True
        else:
            warnings.formatwarning = utils.format_Warning
            warnings.warn("Image and label paths are not correctly set")
            return False

    def build(self):
        """Builds the layout of the widget and creates the following tabs and prompts:

        * Model parameters :

            * Choice of file type for data

            * Dialog for images folder

            * Dialog for label folder

            * Dialog for results folder

            * Model choice

            * Number of samples to extract from images

            * Next tab

            * Close

        * Training parameters :

            * Loss function choice

            * Batch size choice

            * Epochs choice"""

        model_tab = QWidget()
        ###### first tab : model and dataset choices
        model_tab_layout = QVBoxLayout()
        model_tab_layout.setSizeConstraint(QLayout.SetFixedSize)

        model_tab_layout.addWidget(
            utils.combine_blocks(self.filetype_choice, self.lbl_filetype)
        )  # file extension

        model_tab_layout.addWidget(
            utils.combine_blocks(self.btn_image_files, self.lbl_image_files)
        )  # volumes
        if self.data_path != "":
            self.lbl_image_files.setText(self.data_path)

        model_tab_layout.addWidget(
            utils.combine_blocks(self.btn_label_files, self.lbl_label_files)
        )  # labels
        if self.label_path != "":
            self.lbl_label_files.setText(self.label_path)

        # model_tab_layout.addWidget( # TODO : add custom model choice
        #     utils.combine_blocks(self.model_choice, self.lbl_model_choice)
        # )  # model choice

        model_tab_layout.addWidget(
            utils.combine_blocks(self.btn_result_path, self.lbl_result_path)
        )  # results folder
        if self.results_path != "":
            self.lbl_result_path.setText(self.results_path)

        model_tab_layout.addWidget(QLabel("", self))
        model_tab_layout.addWidget(
            utils.combine_blocks(self.model_choice, self.lbl_model_choice)
        )  # model choice

        model_tab_layout.addWidget(
            utils.combine_blocks(self.sample_choice, self.lbl_sample_choice)
        )  # number of samples

        model_tab_layout.addWidget(self.btn_next)
        model_tab_layout.addWidget(QLabel("", self))
        model_tab_layout.addWidget(self.btn_close)

        train_tab = QWidget()
        ####### second tab : training parameters
        train_tab_layout = QVBoxLayout()
        train_tab_layout.setSizeConstraint(QLayout.SetFixedSize)

        train_tab_layout.addWidget(
            utils.combine_blocks(self.loss_choice, self.lbl_loss_choice)
        )  # loss choice
        train_tab_layout.addWidget(
            utils.combine_blocks(self.batch_choice, self.lbl_batch_choice)
        )  # batch size
        train_tab_layout.addWidget(
            utils.combine_blocks(self.epoch_choice, self.lbl_epoch_choice)
        )  # epochs
        train_tab_layout.addWidget(
            utils.combine_blocks(
                self.val_interval_choice, self.lbl_val_interv_choice
            )
        )

        train_tab_layout.addWidget(self.btn_prev)
        train_tab_layout.addWidget(QLabel("", self))
        train_tab_layout.addWidget(self.btn_start)

        model_tab.setLayout(model_tab_layout)
        self.addTab(model_tab, "Model parameters")

        train_tab.setLayout(train_tab_layout)
        self.addTab(train_tab, "Training parameters")

    def show_dialog_lab(self):
        """Shows the  dialog to load label files in a path, loads them (see :doc:model_framework) and changes the widget
        label :py:attr:`self.lbl_label` accordingly"""
        f_name = utils.open_file_dialog(self, self._default_path)

        if f_name:
            self.label_path = f_name
            self.lbl_label.setText(self.label_path)

    def show_dialog_dat(self):
        """Shows the  dialog to load images files in a path, loads them (see :doc:model_framework) and changes the
        widget label :py:attr:`self.lbl_dat` accordingly"""
        f_name = utils.open_file_dialog(self, self._default_path)

        if f_name:
            self.data_path = f_name
            self.lbl_dat.setText(self.data_path)

    def start(self):
        """
        Initiates the :py:func:`train` function as a worker and does the following :

        * Checks that filepaths are set correctly using :py:func:`check_ready`

        * If self.worker is None : creates a worker and starts the training

        * If the button is clicked while training, stops the model once it reaches the next training iteration

        * When the worker yields after a validation step, plots the loss if epoch >= validation_step (to avoid empty plot on first validation)

        * When the worker finishes, clears the memory (tries to for now)

        TODO:

        * Fix memory leak


        Returns: Returns empty immediately if the file paths are not set correctly.

        """

        if not self.check_ready():
            return

        if self.worker is not None:
            if self.worker.is_running:
                pass
            else:
                self.worker.start()
                self.btn_start.setText("Running... Click to stop")
        else:

            self.num_samples = self.sample_choice.value()
            self.batch_size = self.batch_choice.value()
            self.val_interval = self.val_interval_choice.value()
            self.data = self.create_train_dataset_dict()
            self.btn_close.setVisible(False)

            self.worker = self.train()
            self.worker.started.connect(lambda: print("Worker is running..."))
            self.worker.finished.connect(lambda: print("Worker finished"))
            self.worker.finished.connect(
                lambda: self.btn_start.setText("Start")
            )
            self.worker.finished.connect(
                lambda: self.btn_close.setVisible(True)
            )
            self.worker.finished.connect(self.clean_cache)
            if self.get_device().type == "cuda":
                self.worker.finished.connect(self.empty_cuda_cache)

        if self.worker.is_running:
            print("Stop request, waiting for next validation step...")
            self.btn_start.setText("Stopping...")
            self.worker.quit()
        else:
            # self.worker.start()
            self.btn_start.setText("Running...  Click to stop")

    def clean_cache(self):
        """Attempts to clear memory after training"""
        del self.worker
        self.worker = None
        if self.model is not None:
            del self.model
            self.model = None

        del self.epoch_loss_values
        del self.metric_values
        del self.data
        # self.close()
        # del self

    def plot_loss(self, loss, dice_metric):
        """Creates two subplots to plot the training loss and validation metric"""
        with plt.style.context("dark_background"):
            # update loss
            self.train_loss_plot.set_title("Epoch average loss")
            self.train_loss_plot.set_xlabel("Epoch")
            self.train_loss_plot.set_ylabel("Loss")
            x = [i + 1 for i in range(len(loss))]
            y = loss
            self.train_loss_plot.plot(x, y)
            # update metrics
            x = [self.val_interval * (i + 1) for i in range(len(dice_metric))]
            y = dice_metric

            epoch_min = (np.argmax(y) + 1) * self.val_interval
            dice_min = np.max(y)

            self.dice_metric_plot.plot(x, y)
            self.dice_metric_plot.set_title(
                "Validation metric : Mean Dice coefficient"
            )
            self.dice_metric_plot.set_xlabel("Epoch")

            self.dice_metric_plot.scatter(
                epoch_min, dice_min, c="r", label="Maximum Dice coeff."
            )
            self.dice_metric_plot.legend(facecolor="#262930", loc="upper left")
            self.canvas.draw_idle()

    def update_loss_plot(self):
        """
        Updates the plots on subsequent validation steps.
        Creates the plot on the second validation step (epoch == val_interval*2).
        Updates the plot on subsequent validation steps.
        Epoch is obtained from the length of the loss vector.

        Returns: returns empty if the epoch is < than 2 * validation interval.

        """

        # print(len(self.epoch_loss_values))
        # print(self.epoch_loss_values)
        # print(self.metric_values)

        loss = self.epoch_loss_values
        metric = self.metric_values
        epoch = len(loss)
        if epoch < self.val_interval * 2:
            return
        elif epoch == self.val_interval * 2:
            bckgrd_color = (0, 0, 0, 0)  # '#262930'
            with plt.style.context("dark_background"):

                self.canvas = FigureCanvas(Figure(figsize=(10, 3)))
                # loss plot
                self.train_loss_plot = self.canvas.figure.add_subplot(1, 2, 1)
                # dice metric validation plot
                self.dice_metric_plot = self.canvas.figure.add_subplot(1, 2, 2)

                self.canvas.figure.set_facecolor(bckgrd_color)
                self.dice_metric_plot.set_facecolor(bckgrd_color)
                self.train_loss_plot.set_facecolor(bckgrd_color)

                # self.canvas.figure.tight_layout()

                self.canvas.figure.subplots_adjust(
                    left=0.1,
                    bottom=0.2,
                    right=0.95,
                    top=0.9,
                    wspace=0.2,
                    hspace=0,
                )

            self.canvas.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)

            # tab_index = self.addTab(self.canvas, "Loss plot")
            # self.setCurrentIndex(tab_index)
            self._viewer.window.add_dock_widget(self.canvas, area="bottom")
            self.plot_loss(loss, metric)
        else:
            with plt.style.context("dark_background"):

                self.train_loss_plot.cla()
                self.dice_metric_plot.cla()

                self.plot_loss(loss, metric)

    @thread_worker(connect={"yielded": update_loss_plot})
    def train(self):
        """Trains the Pytorch model for num_epochs, with the selected model and data, using the chosen batch size,
        validation interval, loss function, and number of samples."""

        device = self.get_device()
        model_id = self.get_model(self.model_choice.currentText())
        model_name = self.model_choice.currentText()
        data_dicts = self.data
        max_epochs = self.epoch_choice.value()
        loss_function = self.get_loss(self.loss_choice.currentText())
        val_interval = self.val_interval_choice.value()
        batch_size = self.batch_choice.value()
        results_path = self.results_path
        num_samples = self.sample_choice.value()

        model = model_id.get_net()
        model = model.to(device)

        epoch_loss_values = []
        metric_values = []

        # TODO param : % of validation from training set
        train_files, val_files = (
            data_dicts[0 : int(len(data_dicts) * 0.9)],
            data_dicts[int(len(data_dicts) * 0.9) :],
        )
        # print("train/val")
        # print(train_files)
        # print(val_files)
        sample_loader = Compose(
            [
                LoadImaged(keys=["image", "label"]),
                EnsureChannelFirstd(keys=["image", "label"]),
                RandSpatialCropSamplesd(
                    keys=["image", "label"],
                    roi_size=(110, 110, 110),
                    max_roi_size=(120, 120, 120),
                    num_samples=num_samples,
                ),
                SpatialPadd(
                    keys=["image", "label"], spatial_size=(128, 128, 128)
                ),
                EnsureTyped(keys=["image", "label"]),
            ]
        )

        train_transforms = Compose(  # TODO : figure out which ones ?
            [
                RandShiftIntensityd(keys=["image"], offsets=0.7),
                Rand3DElasticd(
                    keys=["image", "label"],
                    sigma_range=(0.3, 0.7),
                    magnitude_range=(0.3, 0.7),
                ),
                RandFlipd(keys=["image", "label"]),
                RandRotate90d(keys=["image", "label"]),
                RandAffined(
                    keys=["image", "label"],
                ),
                EnsureTyped(keys=["image", "label"]),
            ]
        )

        val_transforms = Compose(
            [
                # LoadImaged(keys=["image", "label"]),
                # EnsureChannelFirstd(keys=["image", "label"]),
                EnsureTyped(keys=["image", "label"]),
            ]
        )

        train_ds = PatchDataset(
            data=train_files,
            transform=train_transforms,
            patch_func=sample_loader,
            samples_per_image=num_samples,
        )

        train_loader = DataLoader(
            train_ds,
            batch_size=batch_size,
            shuffle=True,
            num_workers=4,
            collate_fn=pad_list_data_collate,
        )

        val_ds = PatchDataset(
            data=val_files,
            transform=val_transforms,
            patch_func=sample_loader,
            samples_per_image=num_samples,
        )

        val_loader = DataLoader(val_ds, batch_size=batch_size, num_workers=4)

        # TODO : more parameters/flexibility
        post_pred = AsDiscrete(threshold=0.3)
        post_label = EnsureType()

        optimizer = torch.optim.Adam(model.parameters(), 1e-3)
        dice_metric = DiceMetric(include_background=True, reduction="mean")

        best_metric = -1
        best_metric_epoch = -1

        time = utils.get_date_time()

        weights_filename = f"{model_name}_best_metric" + f"_{time}.pth"
        if device.type == "cuda":
            print("\nUsing GPU :")
            print(torch.cuda.get_device_name(0))
        else:
            print("Using CPU")

        for epoch in range(max_epochs):
            print("-" * 10)
            print(f"Epoch {epoch + 1}/{max_epochs}")
            if device.type == "cuda":
                print("Memory Usage:")
                print(
                    "Allocated:",
                    round(torch.cuda.memory_allocated(0) / 1024**3, 1),
                    "GB",
                )
                print(
                    "Cached:   ",
                    round(torch.cuda.memory_reserved(0) / 1024**3, 1),
                    "GB",
                )

            model.train()
            epoch_loss = 0
            step = 0
            for batch_data in train_loader:
                step += 1
                inputs, labels = (
                    batch_data["image"].to(device),
                    batch_data["label"].to(device),
                )
                optimizer.zero_grad()
                outputs = model_id.get_output(model, inputs)
                loss = loss_function(outputs, labels)
                loss.backward()
                optimizer.step()
                epoch_loss += loss.detach().item()
                print(
                    f"{step}/{len(train_ds) // train_loader.batch_size}, "
                    f"Train_loss: {loss.detach().item():.4f}"
                )
            epoch_loss /= step
            self.epoch_loss_values.append(epoch_loss)
            print(f"Epoch {epoch + 1} Average loss: {epoch_loss:.4f}")

            if (epoch + 1) % val_interval == 0:
                model.eval()
                with torch.no_grad():
                    for val_data in val_loader:
                        val_inputs, val_labels = (
                            val_data["image"].to(device),
                            val_data["label"].to(device),
                        )

                        val_outputs = model_id.get_validation(
                            model, val_inputs
                        )

                        pred = decollate_batch(val_outputs)

                        labs = decollate_batch(val_labels)

                        val_outputs = [
                            post_pred(res_tensor) for res_tensor in pred
                        ]

                        val_labels = [
                            post_label(res_tensor) for res_tensor in labs
                        ]

                        # print(len(val_outputs))
                        # print(len(val_labels))

                        dice_metric(y_pred=val_outputs, y=val_labels)

                    metric = dice_metric.aggregate().detach().item()
                    dice_metric.reset()

                    self.metric_values.append(metric)

                    yield self

                    if metric > best_metric:
                        best_metric = metric
                        best_metric_epoch = epoch + 1
                        torch.save(
                            model.state_dict(),
                            os.path.join(results_path, weights_filename),
                        )
                        print("Saved best metric model")
                    print(
                        f"Current epoch: {epoch + 1} Current mean dice: {metric:.4f}"
                        f"\nBest mean dice: {best_metric:.4f} "
                        f"at epoch: {best_metric_epoch}"
                    )
        print("=" * 10)
        print("Done !")
        print(
            f"Train completed, best_metric: {best_metric:.4f} "
            f"at epoch: {best_metric_epoch}"
        )
        print("del")
        # del device
        # del model_id
        # del model_name
        # del model
        # del data_dicts
        # del max_epochs
        # del loss_function
        # del val_interval
        # del batch_size
        # del results_path
        # del num_samples
        # del best_metric
        # del best_metric_epoch

        # self.close()

    def close(self):
        """Close the widget"""
        self._viewer.window.remove_dock_widget(self)
