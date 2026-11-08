import os

from django.core.checks import Warning, register


@register()
def sdk_logging(app_configs, **kwargs):
    """ANTHROPIC_LOG makes the AI SDK log request details; Siteline quiets it, but it shouldn't be set at all."""
    if os.environ.get('ANTHROPIC_LOG', '').strip():
        return [Warning('ANTHROPIC_LOG is set, which asks the AI SDK to log request details.',
                        hint='Unset ANTHROPIC_LOG. Siteline caps the SDK loggers at WARNING regardless.',
                        id='ai.W001')]
    return []
