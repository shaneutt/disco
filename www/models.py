import errno, os

from django.db import models
from django.http import Http404, HttpResponse, HttpResponseServerError

from restapi.resource import Resource, Collection
from restapi.resource import (HttpResponseAccepted,
                              HttpResponseCreated,
                              HttpResponseNoContent,
                              HttpResponseServiceUnavailable)

from discodex import settings
from discodex.mapreduce import Indexer, Queryer, DiscoDBIterator, parsers
from discodex.objects import DataSet, Indices, Index, Keys, Values

from disco.core import Disco
from disco.error import DiscoError
from disco.util import flatten, parse_dir

from discodb import Q

discodex_settings = settings.DiscodexSettings()
disco_master      = discodex_settings['DISCODEX_DISCO_MASTER']
disco_prefix      = discodex_settings['DISCODEX_DISCO_PREFIX']
index_root        = discodex_settings.safedir('DISCODEX_INDEX_ROOT')
disco_master      = Disco(disco_master)

NOT_FOUND, OK, ACTIVE, DEAD = 'unknown job', 'ready', 'active', 'dead'

class IndexCollection(Collection):
    allowed_methods = ('GET', 'POST')

    def delegate(self, request, *args, **kwargs):
        name = str(kwargs.pop('name'))
        return IndexResource(name)(request, *args, **kwargs)

    @property
    def names(self):
        return os.listdir(index_root)

    def __iter__(self):
        for name in self.names:
            yield IndexResource(name)

    def create(self, request, *args, **kwargs):
        dataset = DataSet.loads(request.raw_post_data)
        nr_ichunks = dataset.get('nr_ichunks', 10)
        try:
            job = Indexer(dataset['input'], parsers.parse, parsers.demux, parsers.balance, nr_ichunks)
            job.run(disco_master, disco_prefix)
        except DiscoError, e:
            return HttpResponseServerError("Failed to run indexing job: %s" % e)
        return HttpResponseAccepted(job.name)

    def read(self, request, *args, **kwargs):
        return HttpResponse(Indices(self.names).dumps())

class IndexResource(Collection):
    allowed_methods = ('GET', 'PUT', 'DELETE')

    def __init__(self, name):
        self.name = name

    def delegate(self, request, *args, **kwargs):
        if self.status == NOT_FOUND:
            raise Http404
        property = str(kwargs.pop('property'))
        return getattr(self, property)(request, *args, **kwargs)

    @property
    def exists(self):
        return os.path.exists(self.path)

    @property
    def isdisco(self):
        return self.name.startswith(disco_prefix)

    @property
    def path(self):
        return os.path.join(index_root, self.name)

    @property
    @models.permalink
    def url(self):
        return 'index', (), {'name': self.name}

    @property
    def ichunks(self):
        return Index.loads(open(self.path).read())['ichunks']

    @property
    def keys(self):
        return KeysResource(self)

    @property
    def values(self):
        return ValuesResource(self)

    @property
    def query(self):
        return QueryCollection(self)

    @property
    def status(self):
        if self.exists:
            return OK

        if self.isdisco:
            status, results = disco_master.results(self.name)
            if self.exists:
                return OK

            if status == OK:
                ichunks = list(flatten(parse_dir(result) for result in results))
                self.write(Index(ichunks=ichunks))
                disco_master.clean(self.name)
            return status
        return NOT_FOUND

    def read(self, request, *args, **kwargs):
        status = self.status
        if status == OK:
            return HttpResponse(open(self.path))
        if status == ACTIVE:
            return HttpResponseServiceUnavailable(100)
        if status == DEAD:
            return HttpResponseServerError("Indexing failed.")
        raise Http404

    def update(self, request, *args, **kwargs):
        index = Index.loads(request.raw_post_data)
        self.write(index)
        return HttpResponseCreated(self.url)

    def delete(self, request, *args, **kwargs):
        try:
            os.remove(self.path)
        except OSError, e:
            if e.errno == errno.ENOENT:
                raise Http404
            raise
        else:
            if self.isdisco:
                disco_master.purge(self.name)
        return HttpResponseNoContent()

    def write(self, index):
        from tempfile import NamedTemporaryFile
        handle = NamedTemporaryFile(delete=False)
        handle.write(index.dumps())
        os.rename(handle.name, self.path)

class DiscoDBResource(Resource):
    result_type = Keys
    discodb_method = 'keys'

    def __init__(self, index):
        self.index = index

    @property
    def job(self):
        return DiscoDBIterator(self.index.ichunks, self.discodb_method)

    def read(self, request, *args, **kwargs):
        try:
            job = self.job
            job.run(disco_master, disco_prefix)
        except DiscoError, e:
            return HttpResponseServerError("Failed to run DiscoDB job: %s" % e)

        try:
            results = self.result_type(v for k, v in job.results).dumps()
        except DiscoError, e:
            return HttpResponseServerError("DiscoDB job failed: %s" % e)
        finally:
            disco_master.purge(job.name)

        return HttpResponse(results)

class KeysResource(DiscoDBResource):
    pass

class ValuesResource(DiscoDBResource):
    result_type = Values
    discodb_method = 'values'

class QueryCollection(Collection):
    def __init__(self, index):
        self.index = index

    def delegate(self, request, *args, **kwargs):
        query_path = str(kwargs.pop('query_path'))
        return QueryResource(self.index, query_path)(request, *args, **kwargs)

    def read(self, request, *args, **kwargs):
        return HttpResponse(Values().dumps())

class QueryResource(DiscoDBResource):
    result_type = Values

    def __init__(self, index, query_path):
        self.index = index
        self.query = Q.urlscan(query_path)

    @property
    def job(self):
        return Queryer(self.index.ichunks, self.query)
