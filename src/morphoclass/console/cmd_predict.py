# Copyright © 2022-2022 Blue Brain Project/EPFL
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Implementation of the `morphoclass predict-after-extraction` CLI command."""
import logging
import textwrap

import click

logger = logging.getLogger(__name__)


@click.command(
    name="predict-after-extraction",
    help="Run inference from features directory.",
)
@click.help_option("-h", "--help")
@click.option(
    "-f",
    "--features-dir",
    required=True,
    type=click.Path(exists=True, dir_okay=True),
    help=textwrap.dedent(
        """
    The path to the extracted features of the morphologies to classify
    """
    ).strip(),
)
@click.option(
    "-c",
    "--checkpoint",
    "checkpoint_file",
    required=True,
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    help=textwrap.dedent(
        """
    The path to the pre-trained model checkpoint.
    """
    ).strip(),
)
@click.option(
    "-o",
    "--output-dir",
    required=True,
    type=click.Path(exists=False, file_okay=False, writable=True),
    help="Output directory for the results.",
)
@click.option(
    "-n",
    "--results-name",
    required=False,
    type=click.STRING,
    help="The filename of the results file",
)
def cli(features_dir, checkpoint_file, output_dir, results_name):
    """Run the `morphoclass predict` CLI command.

    Parameters
    ----------
    features_dir
        The path to the features of the morphologies.
    checkpoint_file
        The path to the checkpoint file.
    output_dir
        The path to the output directory.
    results_name
        File prefix for results output files.
    """
    return predict(features_dir, checkpoint_file, output_dir, results_name)


def predict(features_dir, checkpoint_file, output_dir, results_name):
    import json
    import pathlib
    from datetime import datetime

    features_dir = pathlib.Path(features_dir).resolve()
    output_dir = pathlib.Path(output_dir).resolve()
    checkpoint_file = pathlib.Path(checkpoint_file).resolve()
    if results_name is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        results_name = f"results_{timestamp}"
    results_path = output_dir / (results_name + ".json")
    click.secho(f"Features Dir : {features_dir}", fg="yellow")
    click.secho(f"Output file  : {results_path}", fg="yellow")
    click.secho(f"Checkpoint   : {checkpoint_file}", fg="yellow")
    if results_path.exists():
        msg = f'Results file "{results_path}" exists, overwrite? (y/[n]) '
        click.secho(msg, fg="red", bold=True, nl=False)
        response = input()
        if response.strip().lower() != "y":
            click.secho("Stopping.", fg="red")
            return
        else:
            click.secho("You chose to overwrite, proceeding...", fg="red")

    click.secho("✔ Loading checkpoint...", fg="green", bold=True)
    import torch

    from morphoclass.data import MorphologyDataset
    from morphoclass.data.morphology_data import MorphologyData

    checkpoint = torch.load(checkpoint_file, map_location=torch.device("cpu"))
    model_class = checkpoint["model_class"]
    click.secho(f"Model       : {model_class}", fg="yellow")
    if "metadata" in checkpoint:
        timestamp = checkpoint["metadata"]["timestamp"]
        click.secho(f"Created on  : {timestamp}", fg="yellow")

    click.secho("✔ Loading data...", fg="green", bold=True)
    data = []
    for path in sorted(features_dir.glob("*.features")):
        data.append(MorphologyData.load(path))
    dataset = MorphologyDataset(data)
    click.echo(f"> Dataset length: {len(dataset)}")

    click.secho("✔ Computing predictions...", fg="green", bold=True)
    if "ManNet" in model_class:
        logits = predict_gnn(dataset, checkpoint)
        predictions = logits.argmax(axis=1)
    elif "CNN" in model_class:
        logits = predict_cnn(dataset, checkpoint)
        predictions = logits.argmax(axis=1)
    elif "XGB" in model_class:
        predictions = predict_xgb(dataset, checkpoint)
    else:
        click.secho(
            f"Model not recognized: {model_class}. Stopping.",
            fg="red",
            bold=True,
            nl=False,
        )
        return

    click.secho("✔ Exporting results...", fg="green", bold=True)
    prediction_labels = {}
    for sample, sample_pred in zip(dataset.data, predictions):
        sample_path = str(sample.path)
        pred_label = dataset.y_to_label[sample_pred]
        prediction_labels[sample_path] = pred_label

    results = dict()
    results["predictions"] = prediction_labels
    results["checkpoint_path"] = str(checkpoint_file)
    results["model"] = model_class
    with open(results_path, "w") as fp:
        json.dump(results, fp)

    click.secho("✔ Done.", fg="green", bold=True)


def predict_gnn(dataset, checkpoint):
    """Compute predictions with a GNN (ManNet) classifier.

    Parameters
    ----------
    dataset
        The morphology dataset.
    checkpoint
        The model checkpoint.

    Returns
    -------
    logits
        The predictions logits.
    """
    import numpy as np

    import morphoclass.models

    model_cls = getattr(
        morphoclass.models, checkpoint["model_class"].rpartition(".")[2]
    )
    model = model_cls(**checkpoint["model_params"])
    model.load_state_dict(checkpoint["all"]["model"])
    model.eval()
    logits = [model(sample) for sample in dataset]

    return np.array(logits)


def predict_cnn(dataset, checkpoint):
    """Compute predictions with a CNN classifier.

    Parameters
    ----------
    dataset
        The persistence image dataset.
    checkpoint
        The model checkpoint.

    Returns
    -------
    logits
        The predictions logits.
    """
    import numpy as np

    import morphoclass.models

    # Model
    model_cls = getattr(
        morphoclass.models, checkpoint["model_class"].rpartition(".")[2]
    )
    model = model_cls(**checkpoint["model_params"])
    model.load_state_dict(checkpoint["all"]["model"])

    # Evaluation
    logits = [model(sample.image).detach().numpy() for sample in dataset]
    if len(logits) > 0:
        logits = np.concatenate(logits)
    else:
        logits = np.array(logits)

    return logits


def predict_xgb(dataset, checkpoint):
    """Compute predictions with XGBoost classifier.

    Parameters
    ----------
    dataset
        The morphology persistence image dataset.
    checkpoint
        The model checkpoint.

    Returns
    -------
    predictions
        The predictions.
    """
    model = checkpoint["all"]["model"]
    predictions = [
        model.predict(sample.image.numpy().reshape(1, 10000))[0] for sample in dataset
    ]
    return predictions
