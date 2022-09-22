import hashlib
import json
import os
import sys
from zipfile import ZipFile

from .generator import Generator
from ...app import Serverless
from ...dependencies_manager import DependenciesManager

TERRAFORM_OUTPUT_FILE = "terraform.tf.json"
TF_FUNCTION_RESOURCE = "scaleway_function"
TF_NAMESPACE_RESOURCE = "scaleway_function_namespace"


class TerraformGenerator(Generator):
    """
    Terraform Generator

    This class is responsible for generating Terraform Configuration
    """

    def __init__(self, instance: Serverless, deps_manager: DependenciesManager):
        self.instance = instance
        self.deps_manager = deps_manager

    def list_files(self, source):
        zip_files = []

        for path, _subdirs, files in os.walk(source):
            for name in files:
                zip_files.append(os.path.join(path, name))

        return zip_files

    def create_zip_file(self, zip_path, source):
        files = self.list_files(source)

        with ZipFile(zip_path, "w", strict_timestamps=False) as zip:
            for file in files:
                # Allow for safely running the generator multiple times
                if os.path.realpath(file) != os.path.realpath(zip_path):
                    zip.write(file)

    def add_args(self, config, args):
        allowed_args = [  # List of allowed args in terraform function configuration
            "min_scale",
            "max_scale",
            "memory_limit",
            # TODO "timeout" See: https://github.com/scaleway/terraform-provider-scaleway/issues/1476
            "privacy",
            "description",
        ]

        for k, v in args.items():
            if k in allowed_args:
                config[k] = v

    def write(self, path: str):
        version = f"{sys.version_info.major}{sys.version_info.minor}"  # Get the python version from the current env
        config_path = os.path.join(path, TERRAFORM_OUTPUT_FILE)

        config_to_read = config_path

        if not os.path.exists(config_path):
            config_to_read = os.path.join(
                os.path.dirname(__file__), "..", "templates", TERRAFORM_OUTPUT_FILE
            )

        with open(config_to_read, "r") as file:
            config = json.load(file)

        self.deps_manager.generate_package_folder()

        self.create_zip_file(f"{path}/functions.zip", "./")
        with open(f"{path}/functions.zip", "rb") as f:
            zip_bytes = f.read()
            zip_hash = hashlib.sha256(zip_bytes).hexdigest()

        config["resource"][TF_NAMESPACE_RESOURCE] = {
            self.instance.service_name: {
                "name": f"{self.instance.service_name}-function-namespace",
                "description": f"{self.instance.service_name} function namespace",
            }
        }

        if self.instance.env is not None:
            config["resource"][TF_NAMESPACE_RESOURCE][self.instance.service_name][
                "environment_variables"
            ] = self.instance.env

        config["resource"][TF_FUNCTION_RESOURCE] = {}

        for func in self.instance.functions:  # Iterate over the functions
            config["resource"][TF_FUNCTION_RESOURCE][func["function_name"]] = {
                "namespace_id": (
                    "${%s.%s.id}" % (TF_NAMESPACE_RESOURCE, self.instance.service_name)
                ),
                "runtime": f"python{version}",
                "handler": func["handler"],
                "name": func["function_name"],
                "zip_file": "functions.zip",
                "zip_hash": zip_hash,
                "deploy": True,
            }
            self.add_args(
                config["resource"][TF_FUNCTION_RESOURCE][func["function_name"]],
                func["args"],
            )

        functions = [
            fn["function_name"] for fn in self.instance.functions
        ]  # create a list containing the functions name

        config["resource"][TF_FUNCTION_RESOURCE] = {
            key: val
            for key, val in config["resource"][TF_FUNCTION_RESOURCE].items()
            if key in functions
        }  # remove not present functions from configuration file

        with open(config_path, "w") as file:
            json.dump(config, file, indent=2)
