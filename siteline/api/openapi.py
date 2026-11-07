"""
The OpenAPI 3.0 description of the API, built from api.resources.RESOURCES, so a field or filter added
there appears here (and on Settings → API → How to use it) with nothing else to change.
"""
from django.urls import reverse

from .models import SCOPE_LABELS

TYPES = {
    'integer': {'type': 'integer'},
    'string': {'type': 'string'},
    'boolean': {'type': 'boolean'},
    'date': {'type': 'string', 'format': 'date'},
    'date-time': {'type': 'string', 'format': 'date-time'},
    'money': {'type': 'string', 'format': 'decimal', 'pattern': r'^-?\d+\.\d{2}$',
              'description': 'Money, two decimal places, as text.'},
    'number': {'type': 'string', 'format': 'decimal', 'description': 'A decimal number, as text.'},
    'object': {'type': 'object'},
    'array': {'type': 'array', 'items': {}},
}


def field_schema(f):
    schema = dict(TYPES[f.type])
    if f.type == 'array':
        schema['items'] = f.items or {}
    elif f.type == 'object' and f.items:
        schema = dict(f.items)
    if f.description:
        schema['description'] = f.description
    if f.enum:
        schema['enum'] = list(f.enum)
    if f.nullable:
        schema['nullable'] = True
    return schema


def record_schema(resource, detail):
    fields = [f for f in resource.fields if detail or not f.detail_only]
    return {'type': 'object', 'properties': {f.name: field_schema(f) for f in fields},
            'required': [f.name for f in fields]}


def _param(name, schema, description, where='query', required=False):
    return {'name': name, 'in': where, 'required': required, 'description': description, 'schema': schema}


ERROR = {'$ref': '#/components/responses/Error'}


def _strip(path):
    return path[len('/api/v1'):] if path.startswith('/api/v1') else path


def document(server_url):
    from .auth import LIMIT, WINDOW
    from .resources import resources
    from .views import MAX_PAGE_SIZE, PAGE_SIZE
    paths, schemas = {}, {}
    for r in resources():
        key = ''.join(part.capitalize() for part in r.name.split('-'))
        schemas[key] = record_schema(r, False)
        schemas[key + 'Detail'] = record_schema(r, True)
        params = [_param('page', {'type': 'integer', 'minimum': 1, 'default': 1}, 'Page number.'),
                  _param('page_size', {'type': 'integer', 'minimum': 1, 'maximum': MAX_PAGE_SIZE,
                                       'default': PAGE_SIZE}, 'Rows per page.')]
        for f in r.filters:
            schema = dict(TYPES.get(f.type, {'type': 'string'}))
            schema.pop('description', None)
            params.append(_param(f.name, schema, f.description + (f' One of: {", ".join(f.enum)}.' if f.enum else '')))
        scope_note = f' Needs the "{SCOPE_LABELS[r.scope]}" resource group.'
        paths[_strip(reverse(r.list_url_name()))] = {'get': {
            'tags': [r.title], 'summary': f'List {r.title.lower()}', 'description': r.description + scope_note,
            'operationId': f'list_{r.name.replace("-", "_")}', 'parameters': params,
            'responses': {'200': {'description': 'A page of results.', 'content': {'application/json': {'schema': {
                'type': 'object', 'required': ['count', 'page', 'page_size', 'pages', 'next', 'previous', 'results'],
                'properties': {'count': {'type': 'integer'}, 'page': {'type': 'integer'},
                               'page_size': {'type': 'integer'}, 'pages': {'type': 'integer'},
                               'next': {'type': 'string', 'nullable': True},
                               'previous': {'type': 'string', 'nullable': True},
                               'results': {'type': 'array', 'items': {'$ref': f'#/components/schemas/{key}'}}}}}}},
                '400': ERROR, '401': ERROR, '403': ERROR, '404': ERROR, '429': ERROR}}}
        if r.detail:
            detail_path = _strip(reverse(r.detail_url_name(), args=[0])).replace('/0/', '/{id}/')
            detail_params = [_param('id', {'type': 'integer'}, 'The record\'s id.', where='path', required=True)]
            detail_params += [_param('month', {'type': 'string'}, f.description) for f in r.filters if f.name == 'month']
            paths[detail_path] = {'get': {
                'tags': [r.title], 'summary': f'One of the {r.title.lower()}', 'description': r.description + scope_note,
                'operationId': f'get_{r.name.replace("-", "_")}', 'parameters': detail_params,
                'responses': {'200': {'description': 'The record.', 'content': {'application/json': {
                    'schema': {'$ref': f'#/components/schemas/{key}Detail'}}}},
                    '401': ERROR, '403': ERROR, '404': ERROR, '429': ERROR}}}
    download = _strip(reverse('api_document_download', args=[0, 0])).replace('/documents/0/', '/documents/{id}/') \
        .replace('/revisions/0/', '/revisions/{revision_id}/')
    paths[download] = {'get': {
        'tags': ['Drawings and documents'], 'summary': 'Download a revision\'s file',
        'description': 'The file itself, as it was uploaded. Every download is logged against the token. '
                       f'Needs the "{SCOPE_LABELS["documents"]}" resource group.',
        'operationId': 'download_document_revision',
        'parameters': [_param('id', {'type': 'integer'}, 'The document\'s id.', where='path', required=True),
                       _param('revision_id', {'type': 'integer'}, 'The revision\'s id.', where='path', required=True)],
        'responses': {'200': {'description': 'The file.', 'content': {'application/octet-stream': {
            'schema': {'type': 'string', 'format': 'binary'}}}},
            '302': {'description': 'Where cloud storage is used: a short-lived link to the file.'},
            '401': ERROR, '403': ERROR, '404': ERROR, '409': ERROR, '429': ERROR}}}
    paths['/'] = {'get': {'tags': ['About'], 'summary': 'Which company and resource groups this token reads',
                          'operationId': 'index', 'responses': {'200': {'description': 'About the token.'},
                                                                '401': ERROR}}}
    return {
        'openapi': '3.0.3',
        'info': {
            'title': 'Siteline read-only API', 'version': '1.0',
            'description': ('Read your company\'s projects, registers, documents, snags, money and incidents. '
                            'Read only: nothing can be changed through it. Make a token in Settings → API '
                            '(owners and admins) and send it as "Authorization: Bearer slk_...". A token reads one '
                            'company only, as a member of staff would, and only the resource groups it was given. '
                            f'Limit: {LIMIT} calls every {WINDOW // 60} minutes per token. Money is text with two '
                            'decimal places; times are UTC.'),
        },
        'servers': [{'url': server_url}],
        'security': [{'bearer': []}],
        'paths': dict(sorted(paths.items())),
        'components': {
            'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer',
                                           'description': 'A company API token, slk_...'}},
            'schemas': {**schemas, 'Error': {'type': 'object', 'properties': {'error': {'type': 'object', 'properties': {
                'status': {'type': 'integer'}, 'message': {'type': 'string'}}}}}},
            'responses': {'Error': {'description': 'Something went wrong; the message says what to do.',
                                    'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}},
        },
    }
