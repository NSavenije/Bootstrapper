"""Helpers for rendering and applying Jinja2-templated Kubernetes manifests."""
from jinja2 import Environment, PackageLoader

_env = Environment(loader=PackageLoader('bootstrapper', 'templates'))


def render(template_name: str, **ctx) -> str:
    """Render a template from bootstrapper/templates/ and return the result as a string."""
    return _env.get_template(template_name).render(**ctx)
