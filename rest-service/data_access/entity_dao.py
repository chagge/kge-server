#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# coding:utf-8
#
# entity_dao.py: Manages elasticsearch access to search entities
# Copyright (C) 2017 Víctor Fernández Rico <vfrico@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import redis
import json
import elasticsearch.exceptions as es_exceptions
from elasticsearch import Elasticsearch
import data_access.data_access_base as data_access_base


class EntityDTO(data_access_base.DTOClass):
    entity = ""
    label = {}
    description = {}
    alt_label = {}

    def __init__(self, entity_dict):
        self.entity = entity_dict['entity']
        self.label = entity_dict['label']
        self.description = entity_dict['description']
        self.alt_label = entity_dict['alt_label']


class EntityDAO():
    def __init__(self, dataset_type, dataset_id):
        """Data Access Object to interact with autocomplete

        The autocomplete is provided by Elasticsearch, and it is not divided
        by datasets, instead it is divided by dataset type.
        """
        # TODO: Generate an index on elasticsearch with allowed fields
        # The entity must be loaded with a dataset

        # Elasticsearch global params
        self.ELASTIC_ENDPOINT = "http://elasticsearch:9200/"
        self.ELASTIC_AUTH = ("elastic", "changeme")

        # Create Elasticsearch object
        self.es = Elasticsearch(self.ELASTIC_ENDPOINT,
                                http_auth=self.ELASTIC_AUTH)
        self.index = "entities"
        self.type = dataset_type
        self.dataset_id = dataset_id
        # Test if index exists, and if not, creates it
        if not self.es.indices.exists(index=self.index):
            self.generate_index(self.index)

    def generate_index(self, indexName):
        """Generates the index on Elasticsearch

        This method is intended to be used internally. It creates an index
        using certains parameters to get a better search performance.

        :params str indexName: Name of the new index
        """
        body = {'mappings': {
                            self.type: {
                                'properties': {},
                                'dynamic': True
                                }
                             },
                'settings': {
                    'analysis': {
                      'analyzer': {
                        'my_custom_analyzer': {
                          'type': 'custom',
                          'tokenizer': 'standard',
                          'filter': ['lowercase', 'my_ascii_folding']
                        }
                      },
                      'filter': {
                        'my_ascii_folding': {
                            'type': 'asciifolding',
                            'preserve_original': True
                        }
                      }
                    }
                  }}
        suggest_field = {
            'type': 'completion',
            'analyzer': 'my_custom_analyzer',
            'search_analyzer': 'standard',
            'preserve_separators': False,
            'preserve_position_increments': False
        }
        body['mappings'][self.type]['properties'] = {
            'entity': {'type': 'string'},
            'description': {'type': 'object'},
            'label': {'type': 'object'},
            'alt_label': {'type': 'object'},
            'label_suggest': suggest_field
        }
        try:
            self.es.indices.delete(index=indexName)
        except es_exceptions.NotFoundError:
            pass
        self.es.indices.create(index=indexName, body=body)

    def suggest_entity(self, input_string):
        """Calls Elasticsearch to get an autocomplete suggestion

        Given an input string, calls Elasticsearch to get autocomplete
        suggestions based on "completion" suggester. Gives the results filtered
        for an specific dataset (already choosen on constructor).

        :param str input_string: The string to be asked for
        :rtype: list(EntityDTO)
        :returns: a list of EntityDTO
        """
        # Make a query to elasticsearch to find what the user wants
        request = {
          "entities": {
            "text": input_string,
            "completion": {
                "field": "label_suggest"
            }
          }
        }
        resp = self.es.suggest(index=self.index, body=request)

        # Filter entities to return only the entities for self.dataset_id
        entities = []
        try:
            for entity in resp['entities'][0]['options']:
                try:
                    # Append to entities if dataset_id appears on result
                    if self.dataset_id in entity['_source']['datasets']:
                        es_entity = EntityDTO(entity['_source'])
                        entities.append({"entity": es_entity.to_dict(),
                                         "text": entity['text']})
                except KeyError as invalid_key:
                    # If dataset info is not present, just skip it
                    if str(invalid_key) != "datasets":
                        # Re-Raise the error if KeyError is not from 'datasets'
                        raise

        except KeyError as invalid_key:
            if str(invalid_key) == "entities":
                # Will not match with any entity. Return empty list
                entities = []
        return entities

    def insert_entity(self, entity):
        """Insert an entity on Elasticsearch

        Inserts the entity on Elasticsearch and stores the dataset it is, in
        order to get better performance when getting autocomplete predictions

        :param dict entity: The entity to be inserted
        """
        # Suggestions to be stored
        alt_labels = entity['alt_label'].values()
        suggestions = list(entity['label'].values()) +\
            list([item for sublist in alt_labels for item in sublist])
        # Entity document which will be stored on elasticsearch
        full_doc = {"entity": entity['entity'],
                    "description": entity['description'],
                    "label": entity['label'],
                    "alt_label": entity['alt_label'],
                    "label_suggest": suggestions
                    }
        # TODO: Could be useful to use a hash function or similar to avoid
        #       possible URL encoding issues with some entities ID's
        entity_uuid = entity['entity']
        insert = self.es.update(index=self.index, doc_type=self.type,
                                body={"doc": full_doc, "doc_as_upsert": True},
                                id=entity_uuid)

        # Script to update dataset id
        script = {"inline": "",         # Filled below due to high size
                  "lang": "painless",   # Elasticsearch language
                  "params": {
                      "dataset": self.dataset_id
                  }}

        script['inline'] = """if (ctx._source.datasets == null) {
            ctx._source.datasets = [params.dataset]
        } else if(!ctx._source.datasets.contains(params.dataset)) {
            ctx._source.datasets.add(params.dataset)
        }"""
        # TODO: To avoid having two update queries, mix both in one script
        update = self.es.update(index=self.index, doc_type=self.type,
                                body={"script": script}, id=entity_uuid)
