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
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from openrelik_worker_common.file_utils import create_output_file
from openrelik_worker_common.task_utils import create_task_result, get_input_files

from .app import celery

# These are taken from Plaso's extraction tool.
# TODO: Extract these using the ExtractionTool from Plaso.
SUPPORTED_FILE_SIGNATURES = [
    "7z",
    "bzip2",
    "elf",
    "esedb",
    "evt",
    "evtx",
    "ewf_e01",
    "ewf_l01",
    "exe_mz",
    "gzip",
    "jpeg",
    "lnk",
    "msiecf",
    "nk2",
    "olecf",
    "olecf_beta",
    "oxml",
    "pdf",
    "pff",
    "qcow",
    "rar",
    "regf",
    "sqlite2",
    "sqlite3",
    "tar",
    "tar_old",
    "vhdi_footer",
    "vhdi_header",
    "wtcdb_cache",
    "wtcdb_index",
    "zip",
]

# Task name used to register and route the task to the correct queue.
TASK_NAME = "openrelik-worker-extraction.tasks.image_export"

# Task metadata for registration in the core system.
TASK_METADATA = {
    "display_name": "Extract files from disk images",
    "description": "Extract files from disk images using Plaso's Image Export tool.",
    "task_config": [
        {
            "name": "artifacts",
            "label": "Select artifacts to extract",
            "description": "Select one or more forensic artifact definition from the ForensicArtifacts project. These definitions specify files and data relevant to digital forensic investigations.  The selected artifacts will then be extracted from the provided disk image.",
            "type": "artifacts",
            "required": False,
        },
        {
            "name": "filenames",
            "label": "Enter file names to filter on",
            "description": "Filter on file names. This option accepts a comma separated string denoting all file names, e.g. NTUSER.DAT,UsrClass.dat",
            "type": "text",
            "required": False,
        },
        {
            "name": "file_extensions",
            "label": "Enter file extensions to filter on, e.g evtx,exe",
            "description": "Filter on file name extensions. This option accepts multiple multiple comma separated values e.g. evtx,exe",
            "type": "text",
            "required": False,
        },
        {
            "name": "file_signatures",
            "label": "Select file format signatures to filter on",
            "description": "Filter on file format signature identifiers.",
            "type": "autocomplete",
            "items": SUPPORTED_FILE_SIGNATURES,
            "required": False,
        },
    ],
}


@celery.task(bind=True, name=TASK_NAME, metadata=TASK_METADATA)
def extract_task(
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

    def _get_base_command(export_directory):
        """Get the base command for image_export.

        Args:
            export_directory: Directory to export files to.
        """

        return [
            "image_export.py",
            "--no-hashes",
            "--write",
            export_directory,
            "--partitions",
            "all",
            "--volumes",
            "all",
            "--unattended",
        ]

    input_files = get_input_files(pipe_result, input_files or [])
    output_files = []
    commands_to_run = []
    export_directories = []

    # Filters for the artifacts to extract.
    artifact_filter = task_config.get("artifacts")
    filename_filter = task_config.get("filenames")
    file_extension_filter = task_config.get("file_extensions")
    file_signature_filter = task_config.get("file_signatures")

    # If no filters are set, exit early.
    if not any([artifact_filter, filename_filter, file_extension_filter, file_signature_filter]):
        raise RuntimeError("No filters were set. Please set at least one filter.")

    for input_file in input_files:
        # We need to run image_export separately for each filter because it doesn't support
        # combining file and artifact filters.

        # Filter using artifact definitions.
        if artifact_filter:
            export_directory = os.path.join(output_path, uuid4().hex)
            os.mkdir(export_directory)
            command = _get_base_command(export_directory)
            command.extend(["--artifact_filters", ",".join(artifact_filter)])
            commands_to_run.append(command)
            export_directories.append(export_directory)

        # Filter using file properties such as name, extension, and file signature.
        if any([filename_filter, file_extension_filter, file_signature_filter]):
            export_directory = os.path.join(output_path, uuid4().hex)
            os.mkdir(export_directory)
            command = _get_base_command(export_directory)
            if filename_filter:
                command.extend(["--names", filename_filter])
            if file_extension_filter:
                command.extend(["--extensions", file_extension_filter])
            if file_signature_filter:
                command.extend(["--signatures", ",".join(file_signature_filter)])
            commands_to_run.append(command)
            export_directories.append(export_directory)

        for command in commands_to_run:
            # Add the input file path to the command.
            command.append(input_file.get("path"))
            # Execute the command and block until it finishes.
            process = subprocess.Popen(command)
            while process.poll() is None:
                self.send_event("task-progress")
                time.sleep(1)

    for export_directory in export_directories:
        export_directory_path = Path(export_directory)
        extracted_files = [f for f in export_directory_path.glob("**/*") if f.is_file()]
        for file in extracted_files:
            original_path = str(file.relative_to(export_directory_path))
            output_file = create_output_file(
                output_path,
                display_name=file.name,
                original_path=original_path,
                data_type="extraction:image_export:file",
                source_file_id=input_file.get("id"),
            )
            os.rename(file.absolute(), output_file.path)
            output_files.append(output_file.to_dict())

        # Finally clean up the export directory
        shutil.rmtree(export_directory)

    if not output_files:
        raise RuntimeError("image_export didn't create any output files")

    return create_task_result(
        output_files=output_files,
        workflow_id=workflow_id,
    )
