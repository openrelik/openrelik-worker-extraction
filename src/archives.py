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
import shutil
from pathlib import Path

from openrelik_worker_common.archive_utils import extract_archive
from openrelik_worker_common.file_utils import create_output_file
from openrelik_worker_common.task_utils import create_task_result, get_input_files

from .app import celery

# Task name used to register and route the task to the correct queue.
TASK_NAME = "openrelik-worker-extraction.tasks.extract_archive"

# Task metadata for registration in the core system.
TASK_METADATA = {
    "display_name": "Extract files from archives",
    "description": "Extract files from different types of archives (zip, tar, 7z, etc.)",
    "task_config": [
        {
            "name": "file_filter",
            "label": "Select files (glob patterns) to extract",
            "description": "A comma separated list of filenames to extract. Glob patterns are supported. Example: *.txt, *.evtx",
            "type": "text",
            "required": True,
        },
        {
            "name": "archive_password",
            "label": "Password for the input archives",
            "description": "The password needed to extract the input archives",
            "type": "text",
            "required": False,
        },
    ],
}


@celery.task(bind=True, name=TASK_NAME, metadata=TASK_METADATA)
def extract_archive_task(
    self,
    pipe_result: str = None,
    input_files: list = None,
    output_path: str = None,
    workflow_id: str = None,
    task_config: dict = None,
) -> str:
    """Extract archives and create output files for each extracted file.

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
    task_files = []
    archive_password = task_config.get("archive_password", None)
    file_filters = task_config.get("file_filter") or []
    if file_filters:
        file_filters = file_filters.split(",")

    # Send a task progress event to indicate the task has started
    self.send_event("task-progress")

    for input_file in input_files:
        log_file = create_output_file(
            output_path,
            display_name=f"extract_archives_{input_file.get('display_name')}.log",
        )

        (command_string, export_directory) = extract_archive(
            input_file, output_path, log_file.path, file_filters, archive_password
        )

        if os.path.isfile(log_file.path):
            task_files.append(log_file.to_dict())

        export_directory_path = Path(export_directory)
        extracted_files = [file for file in export_directory_path.glob("**/*") if file.is_file()]
        for file in extracted_files:
            original_path = str(file.relative_to(export_directory_path))
            output_file = create_output_file(
                output_path,
                display_name=file.name,
                original_path=original_path,
                data_type="extraction:archive:file",
                source_file_id=input_file.get("id"),
            )
            os.rename(file.absolute(), output_file.path)
            output_files.append(output_file.to_dict())

        # Clean up the export directory
        shutil.rmtree(export_directory)

    if not output_files:
        raise RuntimeError("Archive extractor didn't create any output files")

    return create_task_result(
        output_files=output_files,
        task_files=task_files,
        workflow_id=workflow_id,
        command=command_string,
    )
