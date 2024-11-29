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
import shutil
import tempfile

from pathlib import Path
from uuid import uuid4

from openrelik_worker_common.archive_utils import create_archive
from openrelik_worker_common.file_utils import create_output_file
from openrelik_worker_common.task_utils import create_task_result, get_input_files

from .app import celery

# Task name used to register and route the task to the correct queue.
TASK_NAME = "openrelik-worker-extraction.tasks.file_extract"

# Task metadata for registration in the core system.
TASK_METADATA = {
    "display_name": "Extract Files",
    "description": "Extract files from a disk image",
    "task_config": [
        {
            "name": "filenames",
            "label": "Select filenames to extract",
            "description": "A comma seperated list of filenames to extract.",
            "type": "text",
            "required": True,
        },
        {
            "name": "compress",
            "label": "Compress extracted filenames into zip file",
            "description": "True or False (default False).",
            "type": "text",
            "required": False,
        },
    ],
}


@celery.task(bind=True, name=TASK_NAME, metadata=TASK_METADATA)
def file_extract(
    self,
    pipe_result: str = None,
    input_files: list = None,
    output_path: str = None,
    workflow_id: str = None,
    task_config: dict = None,
) -> str:
    """Run image_export on input files to extract specific filenames.

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
    compress_output = task_config["compress"] or False
    filenames = task_config["filenames"].split(",")
    for filename in filenames:
        for input_file in input_files:
            export_directory = os.path.join(output_path, uuid4().hex)
            os.mkdir(export_directory)

            command = [
                "image_export.py",
                "--unattended",
                "--name",
                filename,
                "--write",
                export_directory,
                "--partitions",
                "all",
                "--volumes",
                "all",
                input_file.get("path"),
            ]

            # Execute the command and block until it finishes.
            subprocess.call(command)

            if compress_output:
                # Compress the extracted files into zip archive and remove the extracted files.
                with tempfile.TemporaryDirectory() as temp_dir:
                    output_zip_file = uuid4().hex + ".zip"
                    zip_file = create_archive(export_directory, os.path.join(temp_dir,output_zip_file), delete_input=True)
                    os.mkdir(export_directory)
                    shutil.move(zip_file, export_directory)

        export_directory_path = Path(export_directory)
        extracted_files = [
            f for f in export_directory_path.glob(f"**/{filename}") if f.is_file()
        ]
        for file in extracted_files:
            original_path = str(file.relative_to(export_directory_path))
            output_file = create_output_file(
                output_path,
                display_name=file.name,
                original_path=original_path,
                data_type=f"worker:openrelik:extraction:image_export:file",
                source_file_id=input_file.get("id"),
            )
            os.rename(file.absolute(), output_file.path)
            output_files.append(output_file.to_dict())

    shutil.rmtree(export_directory)

    if not output_files:
        raise RuntimeError("image_export didn't create any output files")

    return create_task_result(
        output_files=output_files,
        workflow_id=workflow_id,
        command=" ".join(command[:5]),
    )
