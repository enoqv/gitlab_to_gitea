#!/usr/env/bin python

'''
Gitea API wrapper for Python.
API doc: https://try.gitea.io/api/swagger
'''

import parse
import requests

from .resources import resources


class PygiteaRequestException(Exception):
    pass


class API(object):
    '''
    Gitea API wrapper.
    '''

    _api_baseroute = '/api/v1'

    def __init__(self, baseuri, token=None):
        if baseuri.endswith('/'):
            baseuri = baseuri[0:-1]

        self._baseuri = ''.join([baseuri, self._api_baseroute])
        self._token = token

    # Aliases to `call`, this should be prefered entry-points
    def post(self, path, params=None, json=None):
        return self.call(path, method='post', params=params, json=json)

    def get(self, path, params=None):
        return self.call(path, method='get', params=params)

    def delete(self, path, params=None):
        return self.call(path, method='delete', params=params)

    def patch(self, path, params=None, json=None):
        return self.call(path, method='patch', params=params, json=json)

    def put(self, path, params=None, json=None):
        return self.call(path, method='put', params=params, json=json)


    def call(self, path, method, params=None, json=None):
        '''
        Compute, check and execute request to API.
        Returns requests.Response object.
        '''
        # Handle parameters
        if params is None:
            params = {}
        method = method.lower()
        # Check if request needs auth token
        if path.split('/')[1] == 'admin' and self._token is None:
            raise PygiteaRequestException(
                'Resource \'{}\' require an authentification token.'.format(path)
            )
        else:
            params['token'] = self._token

        func = getattr(requests, method)
        final_uri = ''.join([self._baseuri, path])
        return func(final_uri, params=params, json=json)


    def clean_resource_params(self, resource_path, params):
        '''
        Remove params in resource hash already given in resource's uri.
        Example:
            /foo/{bar}/{baz} expect `bar` and `baz`, but since they are in uri we don't want
            to check that again.

        Returns cleaned params hash
        '''
        to_rm = []
        for name in params:
            if '{{{}}}'.format(name) in resource_path:
                to_rm.append(name)

        for key in to_rm:
            params.pop(key, None)

        return params


    def _resource_has_method(self, resource, method):
        '''
        Check if `resource` (path to resource) accept `method` (HTTP method).
        '''
        if method not in resource.keys():
            return False
        return True
