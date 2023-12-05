# -*- coding: utf-8 -*-
from django.shortcuts import render
from django.http import HttpResponse, JsonResponse
from django.template import loader
import json
from amwmeta.xapian import search
import logging
from django.urls import reverse
from amwmeta.utils import paginator, page_list
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from .models import Entry, Agent, Site
from amwmeta.xapian import MycorrhizaIndexer

logger = logging.getLogger(__name__)

def api(request):
    public_only = True
    exclusions = []
    active_sites = { site.id: site.public and site.active for site in Site.objects.all() }
    if request.user.is_authenticated:
        public_only = False
        exclusions = []
        for exclusion in request.user.exclusions.all():
            exclusions.extend(exclusion.as_xapian_queries())
        logger.debug("Exclusions: {}".format(exclusions))


    res = search(
        request.GET,
        public_only=public_only,
        active_sites=active_sites,
        exclusions=exclusions,
    )
    res['total_entries'] = res['pager'].total_entries
    res['pager'] = page_list(res['pager'])
    res['is_authenticated'] = not public_only
    return JsonResponse(res)

@login_required
def api_merge(request, target):
    logger.debug(target)
    out = {}
    data = None
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        out['error'] = "Invalid JSON!";

    if data:
        logger.debug(data)
        canonical = None
        aliases = []
        classes = {
            "entry" : Entry,
            "author" : Agent,
        }
        if target in classes:
            current_class = classes[target]
            for pk in [ x['id'] for x in data ]:
                try:
                    obj = current_class.objects.get(pk=pk)
                    if canonical:
                        aliases.append(obj)
                    else:
                        canonical = obj
                except current_class.DoesNotExist:
                    logger.debug("Invalid entry " + pk)

            if canonical and aliases:
                if canonical.id not in [ x.id for x in aliases ]:
                    logger.info("Merging " + str(aliases) + " into " + str(canonical))
                    reindex = current_class.merge_records(canonical, aliases)
                    indexer = MycorrhizaIndexer()
                    indexer.index_entries(reindex)
                    logger.info(indexer.logs)
                    out['success'] = "Merged!"
                else:
                    out['error'] = "You can't merge an item with itself!"
            else:
                out['error'] = "Bad arguments! Expecting valid canonical and a list of aliases!"
        else:
            out['error'] = 'Invalid path'
    logger.debug(out)
    return JsonResponse(out)
