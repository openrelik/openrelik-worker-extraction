# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import subprocess
import time
import shutil

from pathlib import Path
from uuid import uuid4

from openrelik_worker_common.utils import (
    create_output_file,
    get_input_files,
    task_result,
)

from .app import celery

# Task name used to register and route the task to the correct queue.
TASK_NAME = "openrelik-worker-extraction.tasks.artifact_extract"

# Task metadata for registration in the core system.
TASK_METADATA = {
    "display_name": "Artifact Extraction",
    "description": "Extract artifacts",
    "task_config": [
        {
            "name": "artifacts",
            "label": "Select artifacts to extract",
            "description": "Select one or more forensic artifact definitions from the ForensicArtifacts project. These definitions specify files and data relevant to digital forensic investigations.  The selected artifacts will then be extracted from the provided disk image.",
            "type": "artifacts",
            "required": False,
        },
    ],
}


@celery.task(bind=True, name=TASK_NAME, metadata=TASK_METADATA)
def artifact_extract(
    self,
    pipe_result: str = None,
    input_files: list = None,
    output_path: str = None,
    workflow_id: str = None,
    task_config: dict = None,
) -> str:
    """Run image_export on input files to extract specific artifacts.

    Args:
        pipe_result: Base64-encoded result from the previous Celery task, if any.
        input_files: List of input file dictionaries (unused if pipe_result exists).
        output_path: Path to the output directory.
        workflow_id: ID of the workflow.
        task_config: User configuration for the task.

    Returns:
        Base64-encoded dictionary containing task results.
    """
    input_files = get_input_files(pipe_result, input_files or [])
    output_files = []

    artifacts = task_config["artifacts"]
    if isinstance(artifacts, str):
        artifacts = task_config["artifacts"].split(",")

    for artifact_name in artifacts:
        for input_file in input_files:
            log_file = create_output_file(
                output_path,
                display_name=f"image_export_{artifact_name}.log",
            )

            export_directory = os.path.join(output_path, uuid4().hex)
            os.mkdir(export_directory)

            command = [
                "image_export.py",
                "--no-hashes",
                "--logfile",
                log_file.path,
                "--write",
                export_directory,
                "--partitions",
                "all",
                "--volumes",
                "all",
                "--unattended",
                "--artifact_filters",
                artifact_name,
                input_file.get("path"),
            ]
            command_string = " ".join(command[:5])

            process = subprocess.Popen(command)
            while process.poll() is None:
                if os.path.isfile(log_file.path):
                    with open(log_file.path, "r", encoding="utf-8") as f:
                        log_dict = f.read()
                        self.send_event("task-progress", data=log_dict)
                time.sleep(2)

        if os.path.isfile(log_file.path) and os.stat(log_file.path).st_size > 0:
            output_files.append(log_file.to_dict())

        export_directory_path = Path(export_directory)
        extracted_files = [f for f in export_directory_path.glob("**/*") if f.is_file()]
        for file in extracted_files:
            original_path = str(file.relative_to(export_directory_path))
            output_file = create_output_file(
                output_path,
                display_name=file.name,
                original_path=original_path,
                data_type=f"worker:openrelik:extraction:artifact_extract:artifact:{artifact_name}",
            )
            os.rename(file.absolute(), output_file.path)
            output_files.append(output_file.to_dict())

    shutil.rmtree(export_directory)

    if not output_files:
        raise RuntimeError("image_export didn't create any output files")

    return task_result(
        output_files=output_files,
        workflow_id=workflow_id,
        command=command_string,
    )
