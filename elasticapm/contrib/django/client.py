"""
elasticapm.contrib.django.client
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2011-2017 Elasticsearch

Large portions are
:copyright: (c) 2010 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from __future__ import absolute_import

import logging

import django
from django.conf import settings as django_settings
from django.core.exceptions import DisallowedHost
from django.db import DatabaseError
from django.http import HttpRequest

from elasticapm.base import Client
from elasticapm.contrib.django.utils import iterate_with_template_sources
from elasticapm.utils import compat, get_url_dict
from elasticapm.utils.module_import import import_string
from elasticapm.utils.wsgi import get_environ, get_headers

__all__ = ('DjangoClient',)


default_client_class = 'elasticapm.contrib.django.DjangoClient'
_client = (None, None)


def get_client(client=None):
    """
    Get an ElasticAPM client.

    :param client:
    :return:
    :rtype: elasticapm.base.Client
    """
    global _client

    tmp_client = client is not None
    if not tmp_client:
        config = getattr(django_settings, 'ELASTIC_APM', {})
        client = config.get('CLIENT', default_client_class)

    if _client[0] != client:
        client_class = import_string(client)
        instance = client_class(**get_client_default_config())
        if not tmp_client:
            _client = (client, instance)
        return instance
    return _client[1]


def get_client_default_config():
    return dict(
        include_paths=_get_installed_apps_paths(),
        exclude_paths={'django', 'elasticapm'}
    )


class DjangoClient(Client):
    logger = logging.getLogger('elasticapm.errors.client.django')

    def __init__(self, config=None, **defaults):
        if config is None:
            config = getattr(django_settings, 'ELASTIC_APM', {})
        if 'framework_name' not in defaults:
            defaults['framework_name'] = 'django'
            defaults['framework_version'] = django.get_version()
        super(DjangoClient, self).__init__(config, **defaults)

    def get_user_info(self, request):
        user_info = {}

        if not hasattr(request, 'user'):
            return user_info
        try:
            user = request.user
            if hasattr(user, 'is_authenticated'):
                if callable(user.is_authenticated):
                    user_info['is_authenticated'] = user.is_authenticated()
                else:
                    user_info['is_authenticated'] = bool(user.is_authenticated)
            if hasattr(user, 'id'):
                user_info['id'] = user.id
            if hasattr(user, 'get_username'):
                user_info['username'] = user.get_username()
            elif hasattr(user, 'username'):
                user_info['username'] = user.username

            if hasattr(user, 'email'):
                user_info['email'] = user.email
        except DatabaseError:
            # If the connection is closed or similar, we'll just skip this
            return {}

        return user_info

    def get_data_from_request(self, request):
        if request.method != 'GET':
            try:
                if hasattr(request, 'body'):
                    # Django 1.4+
                    raw_data = request.body
                else:
                    raw_data = request.raw_post_data
                data = raw_data if raw_data else request.POST
            except Exception:
                # assume we had a partial read:
                data = '<unavailable>'
        else:
            data = None

        environ = request.META

        result = {
            'body': data,
            'env': dict(get_environ(environ)),
            'headers': dict(get_headers(environ)),
            'method': request.method,
            'socket': {
                'remote_address': request.META.get('REMOTE_ADDR'),
                'encrypted': request.is_secure()
            },
            'cookies': dict(request.COOKIES),
        }

        if hasattr(request, 'get_raw_uri'):
            # added in Django 1.9
            url = request.get_raw_uri()
        else:
            try:
                # Requires host to be in ALLOWED_HOSTS, might throw a
                # DisallowedHost exception
                url = request.build_absolute_uri()
            except DisallowedHost:
                # We can't figure out the real URL, so we have to set it to
                # DisallowedHost
                result['url'] = {'raw': 'DisallowedHost'}
                url = None
        if url:
            result['url'] = get_url_dict(url)
        return result

    def get_data_from_response(self, response):
        result = {'status_code': response.status_code}

        # Django does not expose a public API to iterate over the headers of a response.
        # Unfortunately, we have to access the private _headers dictionary here, which is
        # a mapping of the form lower-case-header: (Original-Header, value)
        if getattr(response, '_headers', {}):
            result['headers'] = {key: value[1] for key, value in response._headers.items()}
        return result

    def capture(self, event_type, request=None, **kwargs):
        if 'context' not in kwargs:
            kwargs['context'] = context = {}
        else:
            context = kwargs['context']

        is_http_request = isinstance(request, HttpRequest)
        if is_http_request:
            context['request'] = self.get_data_from_request(request)
            context['user'] = self.get_user_info(request)

        result = super(DjangoClient, self).capture(event_type, **kwargs)

        if is_http_request:
            # attach the elasticapm object to the request
            request._elasticapm = {
                'app_name': self.config.app_name,
                'id': result,
            }

        return result

    def _get_stack_info_for_trace(self, frames, extended=True):
        """If the stacktrace originates within the elasticapm module, it will skip
        frames until some other module comes up."""
        frames = list(iterate_with_template_sources(frames, extended))
        i = 0
        while len(frames) > i:
            if 'module' in frames[i] and not (
                    frames[i]['module'].startswith('elasticapm.') or
                    frames[i]['module'] == 'contextlib'
            ):
                return frames[i:]
            i += 1
        return frames

    def send(self, url, **kwargs):
        """
        Serializes and signs ``data`` and passes the payload off to ``send_remote``

        If ``server`` was passed into the constructor, this will serialize the data and pipe it to
        the server using ``send_remote()``.
        """
        if self.config.server_url:
            return super(DjangoClient, self).send(url, **kwargs)
        else:
            self.error_logger.error('No server configured, and elasticapm not installed. Cannot send message')
            return None


class ProxyClient(object):
    """
    A proxy which represents the current client at all times.
    """
    # introspection support:
    __members__ = property(lambda x: x.__dir__())

    # Need to pretend to be the wrapped class, for the sake of objects that care
    # about this (especially in equality tests)
    __class__ = property(lambda x: get_client().__class__)

    __dict__ = property(lambda o: get_client().__dict__)

    __repr__ = lambda: repr(get_client())
    __getattr__ = lambda x, o: getattr(get_client(), o)
    __setattr__ = lambda x, o, v: setattr(get_client(), o, v)
    __delattr__ = lambda x, o: delattr(get_client(), o)

    __lt__ = lambda x, o: get_client() < o
    __le__ = lambda x, o: get_client() <= o
    __eq__ = lambda x, o: get_client() == o
    __ne__ = lambda x, o: get_client() != o
    __gt__ = lambda x, o: get_client() > o
    __ge__ = lambda x, o: get_client() >= o
    if compat.PY2:
        __cmp__ = lambda x, o: cmp(get_client(), o)  # noqa F821
    __hash__ = lambda x: hash(get_client())
    # attributes are currently not callable
    # __call__ = lambda x, *a, **kw: get_client()(*a, **kw)
    __nonzero__ = lambda x: bool(get_client())
    __len__ = lambda x: len(get_client())
    __getitem__ = lambda x, i: get_client()[i]
    __iter__ = lambda x: iter(get_client())
    __contains__ = lambda x, i: i in get_client()
    __getslice__ = lambda x, i, j: get_client()[i:j]
    __add__ = lambda x, o: get_client() + o
    __sub__ = lambda x, o: get_client() - o
    __mul__ = lambda x, o: get_client() * o
    __floordiv__ = lambda x, o: get_client() // o
    __mod__ = lambda x, o: get_client() % o
    __divmod__ = lambda x, o: get_client().__divmod__(o)
    __pow__ = lambda x, o: get_client() ** o
    __lshift__ = lambda x, o: get_client() << o
    __rshift__ = lambda x, o: get_client() >> o
    __and__ = lambda x, o: get_client() & o
    __xor__ = lambda x, o: get_client() ^ o
    __or__ = lambda x, o: get_client() | o
    __div__ = lambda x, o: get_client().__div__(o)
    __truediv__ = lambda x, o: get_client().__truediv__(o)
    __neg__ = lambda x: -(get_client())
    __pos__ = lambda x: +(get_client())
    __abs__ = lambda x: abs(get_client())
    __invert__ = lambda x: ~(get_client())
    __complex__ = lambda x: complex(get_client())
    __int__ = lambda x: int(get_client())
    if compat.PY2:
        __long__ = lambda x: long(get_client())  # noqa F821
    __float__ = lambda x: float(get_client())
    __str__ = lambda x: str(get_client())
    __unicode__ = lambda x: compat.text_type(get_client())
    __oct__ = lambda x: oct(get_client())
    __hex__ = lambda x: hex(get_client())
    __index__ = lambda x: get_client().__index__()
    __coerce__ = lambda x, o: x.__coerce__(x, o)
    __enter__ = lambda x: x.__enter__()
    __exit__ = lambda x, *a, **kw: x.__exit__(*a, **kw)


client = ProxyClient()


def _get_installed_apps_paths():
    """
    Generate a list of modules in settings.INSTALLED_APPS.
    """
    out = set()
    for app in django_settings.INSTALLED_APPS:
        out.add(app)
    return out
