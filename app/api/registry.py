"""One API YAML file maps to one Python implementation module.

Example: docs/rest-project.yaml -> app/repository/rest_project.py.
Operation IDs inside that YAML map to same-named functions in the module.
"""

from app.repository import rest_project, rest_task

CONTRACT_IMPLEMENTATIONS = {
    "rest-project": rest_project,
    "rest-task": rest_task,
}
