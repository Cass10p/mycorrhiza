from django.db import models
from datetime import datetime
from amwmeta.harvest import harvest_oai_pmh, extract_fields
from urllib.parse import urlparse
from datetime import datetime, timezone
from django.db import transaction
from amwmeta.xapian import MycorrhizaIndexer
from django.contrib.auth.models import User
from django.conf import settings
import logging
from amwmeta.sheets import parse_sheet, normalize_records
import random
import requests
import re
import pprint
import hashlib

pp = pprint.PrettyPrinter(indent=2)
logger = logging.getLogger(__name__)

class Library(models.Model):
    name = models.CharField(max_length=255)
    url = models.URLField(max_length=255,
                          blank=True,
                          null=True)
    public = models.BooleanField(default=False, null=False)
    active = models.BooleanField(default=True, null=False)
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)
    def __str__(self):
        return self.name
    class Meta:
        verbose_name_plural = "Libraries"

class Site(models.Model):
    OAI_DC = "oai_dc"
    MARC21 = "marc21"
    OAI_PMH_METADATA_FORMATS = [
        (OAI_DC, "Dublin Core"),
        (MARC21, "MARC XML"),
    ]
    SITE_TYPES = [
        ('amusewiki', "Amusewiki"),
        ('generic', "Generic OAI-PMH"),
        ('csv', "CSV Upload"),
    ]
    library = models.ForeignKey(Library,
                                null=False,
                                on_delete=models.CASCADE,
                                related_name="sites")
    title = models.CharField(max_length=255)
    url = models.URLField(max_length=255)
    last_harvested = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True)
    oai_set = models.CharField(max_length=64,
                               blank=True,
                               null=True)
    oai_metadata_format = models.CharField(max_length=32,
                                           null=True,
                                           choices=OAI_PMH_METADATA_FORMATS)
    site_type = models.CharField(max_length=32, choices=SITE_TYPES, default="generic")
    active = models.BooleanField(default=True, null=False)
    amusewiki_formats = models.JSONField(null=True)
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "{} ({} - {})".format(self.title, self.site_type, self.url)

    def last_harvested_zulu(self):
        dt = self.last_harvested
        if dt:
            # clone
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            return None

    def hostname(self):
        return urlparse(self.url).hostname

    def record_aliases(self):
        aliases = {
            "author": {},
            "language": {},
            "title": {},
            "subtitle": {},
        }
        for al in self.namealias_set.all():
            aliases[al.field_name][al.value_name] = al.value_canonical
        return aliases

    def update_amusewiki_formats(self):
        if self.site_type == 'amusewiki':
            base_uri = urlparse(self.url)
            endpoint = "{0}://{1}/api/format-definitions".format(base_uri.scheme,
                                                                 base_uri.hostname)
            r = requests.get(endpoint)
            if r.status_code == 200:
                self.amusewiki_formats = r.json()
                self.save()
            else:
                logger.debug("GET {0} returned {1}".format(r.url, r.status_code))

    def harvest(self, force=False, oai_set=None):
        self.update_amusewiki_formats()
        url = self.url
        hostname = self.hostname()
        now = datetime.now(timezone.utc)
        opts = {
            "metadataPrefix": self.oai_metadata_format,
        }
        last_harvested = self.last_harvested_zulu()
        logger.debug([ force, last_harvested ])
        set_last_harvested = True
        if last_harvested and not force:
            opts['from'] = last_harvested
        if oai_set:
            opts['set'] = oai_set
            set_last_harvested = False
            opts.pop('from', None)
        elif self.oai_set:
            opts['set'] = self.oai_set

        xapian_records = []
        if force:
            # before deleting, store the entry ids so we can reindex
            # them. Entries without associated datasources will be
            # removed from the index.
            xapian_records = [ i.entry_id for i in self.datasource_set.all() ]
            self.datasource_set.all().delete()

        records = harvest_oai_pmh(url, opts)

        aliases = self.record_aliases()
        counter = 0
        for rec in records:
            counter += 1
            if counter % 10 == 0:
                logger.debug(str(counter) + " records done")
            full_data = rec.get_metadata()
            record = extract_fields(full_data, hostname)
            record['deleted'] = rec.deleted
            record['identifier'] = rec.header.identifier
            record['full_data'] = full_data

            for entry in self.process_harvested_record(record, aliases, now):
                if entry is None:
                    logger.info("Skipping {} deleted? {}, returned None".format(rec.header.identifier, rec.deleted))
                else:
                    xapian_records.append(entry.id)
        # and index
        self.index_harvested_records(xapian_records, force=force, now=now, set_last_harvested=set_last_harvested)

    def index_harvested_records(self, xapian_records, force=False, now=None, set_last_harvested=True):
        indexer = MycorrhizaIndexer(db_path=settings.XAPIAN_DB)
        all_ids = list(set(xapian_records))
        logger.debug("Indexing " + str(all_ids))
        for iid in all_ids:
            try:
                ientry = Entry.objects.get(pk=iid)
                indexer.index_record(ientry.indexing_data())
            except Entry.DoesNotExist:
                logger.info("Entry id {} not found?!".format(iid))

        logs = indexer.logs
        if logs:
            msg = "Total indexed: " + str(len(logs))
            # logger.info(msg)
            logs.append(msg)
            if set_last_harvested:
                logger.info("Setting last harvested to {}".format(now))
                self.last_harvested = now
                self.save()
            self.harvest_set.create(datetime=now, logs="\n".join(logs))

    def process_harvested_record(self, record, aliases, now):
        aggregations = record.pop('aggregations', [])
        entry, ds = self._process_single_harvested_record(record, aliases, now, is_aggregation=False)
        out = []
        if entry and ds:
            out.append(entry)
            for agg in aggregations:
                agg_record = {
                    "title": agg['full_aggregation_name'],
                    "full_data": agg,
                    "identifier": agg['identifier'],
                    "uri": agg.get('linkage'),
                    "uri_label": ds.uri_label,
                    "year_edition": ds.year_edition,
                    "year_first_edition": ds.year_first_edition,
                    "content_type": ds.content_type,
                    "deleted": False,
                    "checksum": agg['checksum'],
                }
                agg_entry, agg_ds = self._process_single_harvested_record(agg_record, aliases, now, is_aggregation=True)
                out.append(agg_entry)
                entry_rel = {
                    "aggregation": agg_entry,
                    "aggregated": entry,
                }
                ds_rel_spec = {
                    "aggregation": agg_ds,
                    "aggregated": ds,
                }
                try:
                    AggregationEntry.objects.get(**entry_rel)
                except AggregationEntry.DoesNotExist:
                    AggregationEntry.objects.create(**entry_rel)

                try:
                    relation = AggregationDataSource.objects.get(**ds_rel_spec)
                except AggregationDataSource.DoesNotExist:
                    relation = AggregationDataSource.objects.create(**ds_rel_spec)

                if agg.get('order'):
                    try:
                        relation.sorting_pos = agg.get('order')
                        relation.save()
                    except ValueError:
                        relation.sorting_pos = None
                        relation.save()
        return out

    def _process_single_harvested_record(self, record, aliases, now, is_aggregation=False):

        authors = []
        languages = []
        for author in record.pop('authors', []):
            author_name = author
            if aliases and aliases.get('author'):
                author_name=aliases['author'].get(author, author)
            obj, was_created = Agent.objects.get_or_create(name=author_name)
            authors.append(obj)

        for language in record.pop('languages', []):
            lang = language[0:3]
            if aliases and aliases.get('language'):
                lang = aliases['language'].get(lang, lang)
            obj, was_created = Language.objects.get_or_create(code=lang)
            languages.append(obj)



        # logger.debug(record)
        identifier = record.pop('identifier')

        ds_attributes = [
            'full_data',
            'uri',
            'uri_label',
            'content_type',
            'shelf_location_code',
            'material_description',
            'year_edition',
            'year_first_edition',
            'description',
        ]
        ds_attrs = { x: record.pop(x, None) for x in ds_attributes }
        ds_attrs['datetime'] = now
        ds_identifiers = {
            "oai_pmh_identifier": identifier,
            "is_aggregation": is_aggregation,
        }
        try:
            ds = self.datasource_set.get(**ds_identifiers)
            for attr, value in ds_attrs.items():
                setattr(ds, attr, value)
            ds.save()
        except DataSource.DoesNotExist:
            ds = self.datasource_set.create(**ds_identifiers, **ds_attrs)

        for f in [ 'title', 'subtitle' ]:
            f_value = record.get(f, '')
            if f_value and len(f_value) > 250:
                f_value = f_value[0:250] + '...'
            if aliases and aliases.get(f):
                f_value = aliases[f].get(f_value, f_value)
            record[f] = f_value

        # if the OAI-PMH record has already a entry attached from a
        # previous run, that's it, just update it.
        entry = ds.entry
        if record.pop('deleted'):
            ds.delete()
            return (entry, None)

        if not record.get('checksum'):
            raise Exception("Expecting checksum in normal entry")

        if not entry:
            # check if there's already a entry with the same checksum.
            try:
                entry = Entry.objects.get(checksum=record['checksum'], is_aggregation=is_aggregation)
            except Entry.DoesNotExist:
                entry = Entry.objects.create(**record, is_aggregation=is_aggregation)
            except Entry.MultipleObjectsReturned:
                entry = Entry.objects.filter(checksum=record['checksum'], is_aggregation=is_aggregation).first()
            ds.entry = entry
            ds.save()

        # update the entry and assign the many to many
        for attr, value in record.items():
            setattr(entry, attr, value)

        entry.authors.set(authors)
        entry.languages.set(languages)
        entry.save()
        return (entry, ds)



# these are a level up from the oai pmh records

class Agent(models.Model):
    name = models.CharField(max_length=255, unique=True)
    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255)
    description = models.TextField()
    canonical_agent = models.ForeignKey(
        'self',
        null=True,
        on_delete=models.SET_NULL,
        related_name="variant_agents",
    )
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    def display_name(self):
        return self.name

    @classmethod
    def merge_records(cls, canonical, aliases):
        canonical.canonical_agent = None
        canonical.save()
        reindex_agents = aliases[:]
        reindex_agents.append(canonical)
        for aliased in aliases:
            aliased.canonical_agent = canonical
            aliased.save()
            for va in aliased.variant_agents.all():
                va.canonical_agent = canonical
                va.save()
                reindex_agents.append(va)
        entries = []
        for agent in reindex_agents:
            for entry in agent.authored_entries.all():
                entries.append(entry)
        return entries

    def __str__(self):
        return self.name

class Language(models.Model):
    code = models.CharField(max_length=4, unique=True, primary_key=True)
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.code

class Entry(models.Model):
    title = models.CharField(max_length=255)
    subtitle = models.CharField(max_length=255, null=True)
    authors = models.ManyToManyField(Agent, related_name="authored_entries")
    languages = models.ManyToManyField(Language)
    checksum = models.CharField(max_length=255)
    is_aggregation = models.BooleanField(default=False)
    canonical_entry = models.ForeignKey(
        'self',
        null=True,
        on_delete=models.SET_NULL,
        related_name="variant_entries",
    )
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)
    indexed_data = models.JSONField(null=True)

    original_entry = models.ForeignKey(
        'self',
        null=True,
        on_delete=models.SET_NULL,
        related_name="translations",
    )

    class Meta:
        verbose_name_plural = "Entries"

    def __str__(self):
        return self.title

    def display_name(self):
        return self.title

    def display_dict(self, library_ids):
        out = {}
        indexed = self.indexed_data
        for f in [ 'id', 'title', 'subtitle' ]:
            out[f] = getattr(self, f)
        out['authors'] = indexed.get('creator')
        out['languages'] = indexed.get('language')
        data_sources = []
        for ds in indexed.get('data_sources'):
            # only the sites explicitely set in the argument
            if ds['library_id'] in library_ids:
                data_sources.append(ds)
        out['data_sources'] = data_sources
        return out

    def display_data(self, library_ids=[]):
        record = self.display_dict(library_ids)
        original = self.original_entry
        if original:
            original_data = original.display_dict(library_ids)

            # if we can't see the entry because there is no data
            # source for that, it does not exist
            if original_data.get('data_sources'):
                record['original_entry'] = original_data
        else:
            original = self

        record['translations'] = []
        # and then the translations
        for tr in original.translations.all():
            if tr.id != self.id:
                tr_data = tr.display_dict(library_ids)
                # ditto. No DS, it does not exist
                if tr_data.get('data_sources'):
                    record['translations'].append(tr_data)

        record['aggregated'] = []
        for agg in original.aggregated_entries.all():
            agg_data = agg.aggregated.display_dict(library_ids)
            if agg_data.get('data_sources'):
                record['aggregated'].append(agg_data)

        record['aggregations'] = []
        for agg in original.aggregation_entries.all():
            agg_data = agg.aggregation.display_dict(library_ids)
            if agg_data.get('data_sources'):
                record['aggregations'].append(agg_data)

        return record

    def indexing_data(self):
        # we index the entries
        data_source_records = []

        # if canonical entry is set, it was merged so it will not be
        # indexed as such.

        if not self.canonical_entry:
            data_source_records = [ xopr for xopr in self.datasource_set.all() ]
            for variant in self.variant_entries.all():
                data_source_records.extend([ xopr for xopr in variant.datasource_set.all() ])

        authors  = []
        for author in self.authors.all():
            real_author = author
            if author.canonical_agent:
                real_author = author.canonical_agent
            authors.append({
                "id": real_author.id,
                "value": real_author.name,
            });

        xapian_data_sources = []
        record_is_public = False
        for topr in data_source_records:
            dsd = topr.indexing_data()
            # at DS level
            dsd['aggregations'] = [ ds.aggregation.indexing_data() for ds in topr.aggregation_data_sources.order_by('sorting_pos').all() ]
            dsd['aggregated']   = [ ds.aggregated.indexing_data() for ds in topr.aggregated_data_sources.order_by('sorting_pos').all() ]
            xapian_data_sources.append(dsd)
            if dsd['public']:
                record_is_public = True

        entry_libraries = {}
        descriptions = []
        dates = {}
        for topr in data_source_records:
            if not entry_libraries.get(topr.site.library_id):
                entry_library = topr.site.library
                entry_libraries[entry_library.id] = {
                    "id": entry_library.id,
                    "value": entry_library.name,
                }
            if topr.description:
                descriptions.append({
                    "id": "d" + str(topr.id),
                    "value": topr.description,
                })
            if topr.year_first_edition:
                dates[topr.year_first_edition] = True
            if topr.year_edition:
                dates[topr.year_edition] = True

        xapian_record = {
            # these are the mapped ones
            "title": [
                { "id": self.id, "value": self.title },
                { "id": self.id, "value": self.subtitle if self.subtitle is not None else "" }
            ],
            "creator": authors,
            "date":     [ { "id": d, "value": d } for d in sorted(list(set(dates))) ],
            "language": [ { "id": l.code, "value": l.code } for l in self.languages.all() ],
            "library": list(entry_libraries.values()),
            "description": descriptions,
            "data_sources": xapian_data_sources,
            "entry_id": self.id,
            "public": record_is_public,
            "last_modified": self.last_modified.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "created": self.created.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "unique_source": 0,
            "aggregations": [ { "id": agg.aggregation.id, "value": agg.aggregation.title } for agg in self.aggregation_entries.all() ],
            "aggregated": [ { "id": agg.aggregated.id, "value": agg.aggregated.title } for agg in self.aggregated_entries.all() ],
            "is_aggregation": self.is_aggregation,
            "aggregate": []
        }
        if self.aggregated_entries.count():
            # if it has aggregated entries, it's an aggregation
            xapian_record['aggregate'].append({ "id": "aggregation", "value": "Aggregation" })
        if self.aggregation_entries.count():
            # if it has aggregation entries, it's an aggregated
            xapian_record['aggregate'].append({ "id": "aggregated", "value": "Aggregated" })

        # logger.debug(xapian_record)
        if len(xapian_record['library']) == 1:
            xapian_record['unique_source'] = xapian_record['library'][0]['id']

        self.indexed_data = xapian_record
        self.save()
        return xapian_record

    @classmethod
    def merge_records(cls, canonical, aliases):
        canonical.canonical_entry = None
        canonical.save()
        reindex = aliases[:]
        for aliased in aliases:
            aliased.canonical_entry = canonical
            aliased.save()
            # update the current variant entries
            for ve in aliased.variant_entries.all():
                ve.canonical_entry = canonical
                ve.save()
                reindex.append(ve)
        # logger.debug(reindex)
        # update the translations
        cls.objects.filter(original_entry__in=reindex).update(original_entry=canonical)
        reindex.append(canonical)
        return reindex

    @classmethod
    def create_virtual_aggregation(cls, name):
        if name:
            sha = hashlib.sha256()
            sha.update(name.encode())
            record = {
                "title": name,
                "checksum": sha.hexdigest(),
                "is_aggregation": True,
            }
            # here there's no uniqueness in the schema, but we enforce it
            try:
                created = cls.objects.get(**record)
            except cls.DoesNotExist:
                created = cls.objects.create(**record)
            except cls.MultipleObjectsReturned:
                logger.debug("Multiple rows found, using the first")
                created = cls.objects.filter(**record).first()
            return created
        return None

    @classmethod
    def aggregate_entries(cls, aggregation_id, *aggregated_ids):
        logger.debug(aggregation_id)
        logger.debug(aggregated_ids)
        # success or error
        out = {}
        try:
            aggregation = cls.objects.get(pk=aggregation_id)
        except cls.DoesNotExist:
            aggregation = None

        if aggregation and aggregation.is_aggregation:
            reindex = [ aggregation_id ]
            aggregated_datasources = []
            for agg_id in aggregated_ids:
                try:
                    aggregated = cls.objects.get(pk=agg_id)
                    if not aggregated.is_aggregation:
                        reindex.append(agg_id)
                        aggregated_datasources.extend([ ds for ds in aggregated.datasource_set.all() ])
                        entry_rel = {
                            "aggregation": aggregation,
                            "aggregated": aggregated,
                        }
                        try:
                            rel = AggregationEntry.objects.get(**entry_rel)
                        except AggregationEntry.DoesNotExist:
                            rel = AggregationEntry.objects.create(**entry_rel)
                        logger.info("Created AggregationEntry {}".format(rel.id))

                except cls.DoesNotExist:
                    pass

            # now we have all the aggregated datasources.
            # check if we have a real or virtual DS in the aggregation
            for ds in aggregated_datasources:
                # search all the DS for this aggregation entry with a matching site
                agg_datasources = [ x for x in aggregation.datasource_set.filter(site_id=ds.site_id).all() ]
                if agg_datasources:
                    logger.debug("{} already has an aggregation datasource".format(ds.oai_pmh_identifier))
                else:
                    logger.info("Creating virtual DS for {}".format(ds.oai_pmh_identifier))
                    agg_ds = DataSource.objects.create(
                        site_id=ds.site_id,
                        oai_pmh_identifier="virtual:site-{}:aggregation-{}".format(ds.site_id, aggregation.id),
                        datetime=aggregation.last_modified,
                        entry_id=aggregation.id,
                        is_aggregation=True,
                        full_data={},
                    )
                    agg_datasources.append(agg_ds)

                for agg_ds in agg_datasources:
                    agg_rel = {
                        "aggregation": agg_ds,
                        "aggregated": ds,
                    }
                    try:
                        rel = AggregationDataSource.objects.get(**agg_rel)
                    except AggregationDataSource.DoesNotExist:
                        rel = AggregationDataSource.objects.create(**agg_rel)
                        logger.info("Created AggregationDataSource {}".format(rel.id))

            indexer = MycorrhizaIndexer(db_path=settings.XAPIAN_DB)
            logger.debug("Reindexing " + pp.pformat(reindex))
            indexer.index_entries(Entry.objects.filter(id__in=reindex).all())
            if len(reindex) > 1:
                out['success'] = "Reindexed {} items".format(len(reindex))
            else:
                out['error'] = "Nothing to do. Expecting an aggregation and normal entries"
        else:
            out['error'] = "First item is not an aggregation"
        return out


# the OAI-PMH records will keep the URL of the record, so a entry can
# have multiple ones because it's coming from more sources.

# DataSource
class DataSource(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    oai_pmh_identifier = models.CharField(max_length=2048)
    datetime = models.DateTimeField()
    full_data = models.JSONField()

    entry = models.ForeignKey(Entry, null=True, on_delete=models.SET_NULL)

    description = models.TextField(null=True)
    year_edition = models.IntegerField(null=True)
    year_first_edition = models.IntegerField(null=True)

    # if digital, provide the url
    uri = models.URLField(max_length=2048, null=True)
    uri_label = models.CharField(max_length=2048, null=True)
    content_type = models.CharField(max_length=128, null=True)
    # if this is the real book, if it exists: phisical description and call number
    material_description = models.TextField(null=True)
    shelf_location_code = models.CharField(max_length=255, null=True)
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)
    is_aggregation = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['site', 'oai_pmh_identifier'], name='unique_site_oai_pmh_identifier'),
        ]
    def __str__(self):
        return self.oai_pmh_identifier

    def amusewiki_base_url(self):
        site = self.site
        if site.site_type == 'amusewiki':
            return re.sub(r'((\.[a-z0-9]+)+)$',
                          '',
                          self.uri)
        else:
            return None

    def get_remote_file(self, ext):
        amusewiki_url = self.amusewiki_base_url()
        if amusewiki_url:
            logger.debug("AMW url is " + amusewiki_url)
            return requests.get(amusewiki_url + ext)
        else:
            return None

    def full_text(self):
        amusewiki_url = self.amusewiki_base_url()
        if amusewiki_url:
            r = requests.get(amusewiki_url + '.bare.html')
            if r.status_code == 200:
                r.encoding = 'UTF-8'
                return r.text
        else:
            return None

    def indexing_data(self):
        site = self.site
        library = site.library
        original_entry = self.entry
        ds = {
            "data_source_id": self.id,
            "identifier": self.oai_pmh_identifier,
            "title": original_entry.title,
            "subtitle": original_entry.subtitle,
            "authors": [ author.name for author in original_entry.authors.all() ],
            "languages": [ lang.code for lang in original_entry.languages.all() ],
            "uri": self.uri,
            "uri_label": self.uri_label,
            "content_type": self.content_type,
            "shelf_location_code": self.shelf_location_code,
            "public": False,
            "site_name": site.title,
            "site_id": site.id,
            "site_type": site.site_type,
            "library_id" : library.id,
            "library_name": library.name,
            "description": self.description,
            "year_edition": self.year_edition,
            "year_first_edition": self.year_first_edition,
            "material_description": self.material_description,
            "downloads": [] if self.is_aggregation else site.amusewiki_formats,
            "entry_id": original_entry.id,
        }
        if library.active and library.public:
            ds['public'] = True
        return ds

# linking table between entries for aggregations

class AggregationEntry(models.Model):
    aggregation = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="aggregated_entries")
    aggregated  = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="aggregation_entries")
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['aggregation', 'aggregated'],
                name='unique_entry_aggregation_aggregated'
            ),
        ]
        verbose_name_plural = "Aggregation Entries"

# linking table between datasource for aggregations

class AggregationDataSource(models.Model):
    aggregation = models.ForeignKey(DataSource, on_delete=models.CASCADE, related_name="aggregated_data_sources")
    aggregated  = models.ForeignKey(DataSource, on_delete=models.CASCADE, related_name="aggregation_data_sources")
    sorting_pos = models.IntegerField(null=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['aggregation', 'aggregated'],
                name='unique_data_source_aggregation_aggregated'
            ),
        ]

class NameAlias(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    field_name = models.CharField(
        max_length=32,
        choices=[
            ('author', 'Author'),
            ('title', 'Title'),
            ('subtitle', 'Subtitle'),
            ('language', 'Language')
        ]
    )
    value_name = models.CharField(max_length=255, blank=False)
    value_canonical = models.CharField(max_length=255, blank=False)
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['site', 'field_name', 'value_name'],
                name='unique_site_field_name_value_name'
            ),
        ]
        verbose_name_plural = "Name Aliases"

    def __str__(self):
        return self.value_name + ' => ' + self.value_canonical

# this is just to trace the harvesting
class Harvest(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    datetime = models.DateTimeField()
    logs = models.TextField()
    def __str__(self):
        return self.site.title + ' Harvest ' + self.datetime.strftime('%Y-%m-%dT%H:%M:%SZ')

class Exclusion(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="exclusions")
    exclude_library = models.ForeignKey(Library, null=True, on_delete=models.SET_NULL)
    exclude_author = models.ForeignKey(Agent, null=True, on_delete=models.SET_NULL)
    exclude_entry  = models.ForeignKey(Entry, null=True, on_delete=models.SET_NULL)
    comment = models.TextField()
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)
    def as_xapian_queries(self):
        queries = []
        if self.exclude_library:
            queries.append(('library', self.exclude_library_id))
        if self.exclude_author:
            queries.append(('creator', self.exclude_author_id))
        if self.exclude_entry:
            queries.append(('entry', self.exclude_entry_id))
        return queries
    def as_json_data(self):
        out = {
            "id": self.id,
            "comment": self.comment,
            "created": self.created.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        if self.exclude_library:
            out['type'] = 'library'
            out['target'] = self.exclude_library.name
        elif self.exclude_author:
            out['type'] = 'author'
            out['target'] = self.exclude_author.name
        elif self.exclude_entry:
            out['type'] = 'entry'
            out['target'] = self.exclude_entry.title
        return out

def spreadsheet_upload_directory(instance, filename):
    choices = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "spreadsheets/{0}-{1}.csv".format(int(datetime.now().timestamp()),
                                             "".join(random.choice(choices) for i in range(20)))

class SpreadsheetUpload(models.Model):
    CSV_TYPES = [
        ('calibre', 'Calibre'),
        ('abebooks_home_base', 'Abebooks Home Base'),
    ]
    user = models.ForeignKey(
        User,
        null=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_spreadsheets",
    )
    spreadsheet = models.FileField(upload_to=spreadsheet_upload_directory)
    comment = models.TextField(blank=True)
    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    csv_type = models.CharField(max_length=32, choices=CSV_TYPES)
    replace_all = models.BooleanField(default=False, null=False)
    processed = models.DateTimeField(null=True, blank=True)

    def validate_csv(self):
        return parse_sheet(self.csv_type, self.spreadsheet.path, sample=True)

    def process_csv(self):
        now = datetime.now(timezone.utc)
        records = normalize_records(self.csv_type,
                                    parse_sheet(self.csv_type, self.spreadsheet.path))
        site = self.site
        hostname = site.hostname()
        aliases = site.record_aliases()
        xapian_records = []
        if self.replace_all:
            # see above in site.harvest()
            xapian_records = [ i.entry_id for i in site.datasource_set.all() ]
            site.datasource_set.all().delete()

        logger.debug("Reindexing: {}".format(xapian_records))
        for full in records:
            # logger.debug(full)
            record = extract_fields(full, hostname)
            if full.get('identifier'):
                record['identifier'] = 'ss:{}:{}'.format(hostname, full['identifier'][0])
            record['full_data'] = full
            record['deleted'] = False
            for entry in site.process_harvested_record(record, aliases, now):
                xapian_records.append(entry.id)
        site.index_harvested_records(xapian_records, force=self.replace_all, now=now)
        self.processed = now
        self.save()

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    libraries = models.ManyToManyField(Library)
    created = models.DateTimeField(auto_now_add=True)
    last_modified = models.DateTimeField(auto_now=True)
