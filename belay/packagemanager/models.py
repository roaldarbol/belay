"""Pydantic models for validation Belay configuration.
"""

from functools import partial
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel as PydanticBaseModel
from pydantic import validator

validator_reuse = partial(validator, allow_reuse=True)
prevalidator_reuse = partial(validator_reuse, pre=True)


class BaseModel(PydanticBaseModel):
    class Config:
        allow_mutation = False


class DependencySourceConfig(BaseModel):
    uri: str
    rename_to_init: bool = False


DependencyList = List[DependencySourceConfig]


def _dependencies_name_validator(dependencies) -> dict:
    for group_name in dependencies:
        if not group_name.isidentifier():
            raise ValueError("Dependency group name must be a valid python identifier.")
    return dependencies


def _dependencies_preprocessor(dependencies) -> dict[str, List[dict]]:
    """Preprocess various dependencies based on dtype.

    * ``str`` -> single dependency that may get renamed to __init__.py, if appropriate.
    * ``list`` -> list of dependencies. If an element is a str, it will not
      get renamed to __init__.py.
    * ``dict`` -> full dependency specification.
    """
    out = {}
    for group_name, group_value in dependencies.items():
        if isinstance(group_value, str):
            group_value = [
                {
                    "uri": group_value,
                    "rename_to_init": True,
                }
            ]
        elif isinstance(group_value, list):
            group_value_out = []
            for elem in group_value:
                if isinstance(elem, str):
                    group_value_out.append(
                        {
                            "uri": elem,
                        }
                    )
                elif isinstance(elem, list):
                    raise ValueError(
                        "Cannot have double nested lists in dependency specification."
                    )
                elif isinstance(elem, (dict, DependencySourceConfig)):
                    group_value_out.append(elem)
                else:
                    raise NotImplementedError
            group_value = group_value_out
        elif isinstance(group_value, dict):
            group_value = [group_value]
        elif isinstance(group_value, DependencySourceConfig):
            # Nothing to do
            pass
        else:
            raise ValueError

        out[group_name] = group_value

    return out


class GroupConfig(BaseModel):
    optional: bool = False
    dependencies: Dict[str, DependencyList] = {}

    ##############
    # VALIDATORS #
    ##############
    _v_dependencies_preprocessor = prevalidator_reuse("dependencies")(
        _dependencies_preprocessor
    )
    _v_dependencies_names = validator_reuse("dependencies")(
        _dependencies_name_validator
    )


class BelayConfig(BaseModel):
    """Configuration schema under the ``tool.belay`` section of ``pyproject.toml``."""

    # Name/Folder of project's primary micropython code.
    name: Optional[str] = None

    # "main" dependencies
    dependencies: Dict[str, DependencyList] = {}

    # Path to where dependency groups should be stored relative to project's root.
    dependencies_path: Path = Path(".belay/dependencies")

    # Other dependencies
    group: Dict[str, GroupConfig] = {}

    ##############
    # VALIDATORS #
    ##############
    _v_dependencies_preprocessor = prevalidator_reuse("dependencies")(
        _dependencies_preprocessor
    )
    _v_dependencies_names = validator_reuse("dependencies")(
        _dependencies_name_validator
    )

    @validator("group")
    def main_not_in_group(cls, v):
        if "main" in v:
            raise ValueError(
                'Specify "main" group dependencies under "tool.belay.dependencies", '
                'not "tool.belay.group.main.dependencies"'
            )
        return v