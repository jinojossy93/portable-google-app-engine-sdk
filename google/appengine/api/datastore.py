#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""The Python datastore API used by app developers.

Defines Entity, Query, and Iterator classes, as well as methods for all of the
datastore's calls. Also defines conversions between the Python classes and
their PB counterparts.

The datastore errors are defined in the datastore_errors module. That module is
only required to avoid circular imports. datastore imports datastore_types,
which needs BadValueError, so it can't be defined in datastore.
"""






import logging
import re
import string
import sys
import traceback
from xml.sax import saxutils

from google.appengine.api import api_base_pb
from google.appengine.datastore import datastore_pb
from google.appengine.api import apiproxy_stub_map
from google.appengine.api import datastore_errors
from google.appengine.api import datastore_types
from google.appengine.runtime import apiproxy_errors
from google.appengine.datastore import entity_pb

_LOCAL_APP_ID = datastore_types._LOCAL_APP_ID

TRANSACTION_RETRIES = 10

Key = datastore_types.Key
typename = datastore_types.typename

_txes = {}
_tx_entity_groups = {}


def NormalizeAndTypeCheck(arg, types):
  """Normalizes and type checks the given argument.

  Args:
    arg: an instance, tuple, list, iterator, or generator of the given type(s)
    types: allowed type or tuple of types

  Returns:
    A (list, bool) tuple. The list is a normalized, shallow copy of the
    argument. The boolean is True if the argument was a sequence, False
    if it was a single object.

  Raises:
    AssertionError: types includes list or tuple.
    BadArgumentError: arg is not an instance or sequence of one of the given
    types.
  """
  if not isinstance(types, (list, tuple)):
    types = (types,)

  assert list not in types and tuple not in types

  if isinstance(arg, types):
    return ([arg], False)
  else:
    try:
      for val in arg:
        if not isinstance(val, types):
          raise datastore_errors.BadArgumentError(
              'Expected one of %s; received %s (a %s).' %
              (types, val, typename(val)))
    except TypeError:
      raise datastore_errors.BadArgumentError(
          'Expected an instance or sequence of %s; received %s (a %s).' %
          (types, arg, typename(arg)))

    return (list(arg), True)


def NormalizeAndTypeCheckKeys(keys):
  """Normalizes and type checks that the given argument is a valid key or keys.

  A wrapper around NormalizeAndTypeCheck() that accepts strings, Keys, and
  Entities, and normalizes to Keys.

  Args:
    keys: a Key or sequence of Keys

  Returns:
    A (list of Keys, bool) tuple. See NormalizeAndTypeCheck.

  Raises:
    BadArgumentError: arg is not an instance or sequence of one of the given
    types.
  """
  keys, multiple = NormalizeAndTypeCheck(keys, (basestring, Entity, Key))

  keys = [_GetCompleteKeyOrError(key) for key in keys]

  return (keys, multiple)


def Put(entities):
  """Store one or more entities in the datastore.

  The entities may be new or previously existing. For new entities, Put() will
  fill in the app id and key assigned by the datastore.

  If the argument is a single Entity, a single Key will be returned. If the
  argument is a list of Entity, a list of Keys will be returned.

  Args:
    entities: Entity or list of Entities

  Returns:
    Key or list of Keys

  Raises:
    TransactionFailedError, if the Put could not be committed.
  """
  entities, multiple = NormalizeAndTypeCheck(entities, Entity)

  entity_group = entities[0]._entity_group()
  for entity in entities:
    if not entity.kind() or not entity.app():
      raise datastore_errors.BadRequestError(
          'App and kind must not be empty, in entity: %s' % entity)
    elif entity._entity_group() != entity_group:
      raise datastore_errors.BadRequestError(
          'All entities must be in the same entity group.')

  req = datastore_pb.PutRequest()
  req.entity_list().extend([e._ToPb() for e in entities])
  _MaybeSetupTransaction(req, entities[0])

  resp = datastore_pb.PutResponse()
  try:
    apiproxy_stub_map.MakeSyncCall('datastore_v3', 'Put', req, resp)
  except apiproxy_errors.ApplicationError, err:
    raise _ToDatastoreError(err)

  keys = resp.key_list()
  num_keys = len(keys)
  num_entities = len(entities)
  if num_keys != num_entities:
    raise datastore_errors.InternalError(
        'Put accepted %d entities but returned %d keys.' %
        (num_entities, num_keys))

  for entity, key in zip(entities, keys):
    entity._Entity__key._Key__reference.CopyFrom(key)

  if multiple:
    return [Key._FromPb(k) for k in keys]
  else:
    return Key._FromPb(resp.key(0))


def Get(keys):
  """Retrieves one or more entities from the datastore.

  Retrieves the entity or entities with the given key(s) from the datastore
  and returns them as fully populated Entity objects, as defined below. If
  there is an error, raises a subclass of datastore_errors.Error.

  If keys is a single key or string, an Entity will be returned, or
  EntityNotFoundError will be raised if no existing entity matches the key.

  However, if keys is a list or tuple, a list of entities will be returned
  that corresponds to the sequence of keys. It will include entities for keys
  that were found and None placeholders for keys that were not found.

  Args:
    # the primary key(s) of the entity(ies) to retrieve
    keys: Key or string or list of Keys or strings

  Returns:
    Entity or list of Entity objects
  """
  keys, multiple = NormalizeAndTypeCheckKeys(keys)

  req = datastore_pb.GetRequest()
  req.key_list().extend([key._Key__reference for key in keys])
  _MaybeSetupTransaction(req, keys[0])

  resp = datastore_pb.GetResponse()
  try:
    apiproxy_stub_map.MakeSyncCall('datastore_v3', 'Get', req, resp)
  except apiproxy_errors.ApplicationError, err:
    raise _ToDatastoreError(err)

  entities = []
  for group in resp.entity_list():
    if group.has_entity():
      entities.append(Entity._FromPb(group.entity()))
    else:
      entities.append(None)

  if multiple:
    return entities
  else:
    if entities[0] is None:
      raise datastore_errors.EntityNotFoundError()
    return entities[0]


def Delete(keys):
  """Deletes one or more entities from the datastore. Use with care!

  Deletes the given entity(ies) from the datastore. You can only delete
  entities from your app. If there is an error, raises a subclass of
  datastore_errors.Error.

  Args:
    # the primary key(s) of the entity(ies) to delete
    keys: Key or string or list of Keys or strings

  Raises:
    TransactionFailedError, if the Put could not be committed.
  """
  keys, _ = NormalizeAndTypeCheckKeys(keys)

  entity_group = keys[0]._entity_group()
  for key in keys:
    if key._entity_group() != entity_group:
      raise datastore_errors.BadRequestError(
          'All keys must be in the same entity group.')

  req = datastore_pb.DeleteRequest()
  req.key_list().extend([key._Key__reference for key in keys])
  _MaybeSetupTransaction(req, keys[0])

  resp = api_base_pb.VoidProto()
  try:
    apiproxy_stub_map.MakeSyncCall('datastore_v3', 'Delete', req, resp)
  except apiproxy_errors.ApplicationError, err:
    raise _ToDatastoreError(err)


class Entity(dict):
  """A datastore entity.

  Includes read-only accessors for app id, kind, and primary key. Also
  provides dictionary-style access to properties.
  """
  def __init__(self, kind, parent=None, _app=None, name=None):
    """Constructor. Takes the kind and transaction root, which cannot be
    changed after the entity is constructed, and an optional parent. Raises
    BadArgumentError or BadKeyError if kind is invalid or parent is not an
    existing Entity or Key in the datastore.

    Args:
      # this entity's kind
      kind: string
      # if provided, this entity's parent. Its key must be complete.
      parent: Entity or Key
      # if provided, this entity's name.
      name: string
    """
    ref = entity_pb.Reference()
    if _app is not None:
      datastore_types.ValidateString(_app, '_app',
                                     datastore_errors.BadArgumentError)
      ref.set_app(_app)
    else:
      ref.set_app(_LOCAL_APP_ID)

    datastore_types.ValidateString(kind, 'kind',
                                   datastore_errors.BadArgumentError)

    if parent is not None:
      if _app is not None and _app != parent.app():
        raise datastore_errors.BadArgumentError(
            "_app %s doesn't match parent's app %s" % (_app, parent.app()))
      ref.CopyFrom(_GetCompleteKeyOrError(parent)._Key__reference)

    last_path = ref.mutable_path().add_element()
    last_path.set_type(kind.encode('utf-8'))

    if name is not None:
      datastore_types.ValidateString(name, 'name')
      if name[0] in string.digits:
        raise datastore_errors.BadValueError('name cannot begin with a digit')
      last_path.set_name(name.encode('utf-8'))

    self.__key = Key._FromPb(ref)

  def app(self):
    """Returns the name of the application that created this entity, a
    string.
    """
    return self.__key.app()

  def kind(self):
    """Returns this entity's kind, a string.
    """
    return self.__key.kind()

  def key(self):
    """Returns this entity's primary key, a Key instance.
    """
    return self.__key

  def parent(self):
    """Returns this entity's parent, as a Key. If this entity has no parent,
    returns None.
    """
    return self.key().parent()

  def _entity_group(self):
    """Returns this entitys's entity group as a Key, or None.
    """
    return self.key()._entity_group()

  def __setitem__(self, name, value):
    """Implements the [] operator. Used to set property value(s).

    If the property name is the empty string or not a string, raises
    BadPropertyError. If the value is not a supported type, raises
    BadValueError.
    """
    datastore_types.ToPropertyPb(name, value)
    dict.__setitem__(self, name, value)

  def setdefault(self, name, value):
    """If the property exists, returns its value. Otherwise sets it to value.

    If the property name is the empty string or not a string, raises
    BadPropertyError. If the value is not a supported type, raises
    BadValueError.
    """
    datastore_types.ToPropertyPb(name, value)
    return dict.setdefault(self, name, value)

  def update(self, other):
    """Updates this entity's properties from the values in other.

    If any property name is the empty string or not a string, raises
    BadPropertyError. If any value is not a supported type, raises
    BadValueError.
    """
    for name, value in other.items():
      self.__setitem__(name, value)

  def copy(self):
    """The copy method is not supported.
    """
    raise NotImplementedError('Entity does not support the copy() method.')

  def ToXml(self):
    """Returns an XML representation of this entity. Atom and gd:namespace
    properties are converted to XML according to their respective schemas. For
    more information, see:

      http://www.atomenabled.org/developers/syndication/
      http://code.google.com/apis/gdata/common-elements.html

    This is *not* optimized. It shouldn't be used anywhere near code that's
    performance-critical.
    """
    xml = u'<entity kind=%s' % saxutils.quoteattr(self.kind())
    if self.__key.has_id_or_name():
      xml += ' key=%s' % saxutils.quoteattr(str(self.__key))
    xml += '>'
    if self.__key.has_id_or_name():
      xml += '\n  <key>%s</key>' % self.__key.ToTagUri()


    properties = self.keys()
    if properties:
      properties.sort()
      xml += '\n  ' + '\n  '.join(self._PropertiesToXml(properties))

    xml += '\n</entity>\n'
    return xml

  def _PropertiesToXml(self, properties):
    """ Returns a list of the XML representations of each of the given
    properties. Ignores properties that don't exist in this entity.

    Arg:
      properties: string or list of strings

    Returns:
      list of strings
    """
    xml_properties = []

    for propname in properties:
      if not self.has_key(propname):
        continue

      propname_xml = saxutils.quoteattr(propname)

      values = self[propname]
      if not isinstance(values, list):
        values = [values]

      proptype = datastore_types.PropertyTypeName(values[0])
      proptype_xml = saxutils.quoteattr(proptype)

      escaped_values = self._XmlEscapeValues(propname)
      open_tag = u'<property name=%s type=%s>' % (propname_xml, proptype_xml)
      close_tag = u'</property>'
      xml_properties += [open_tag + val + close_tag for val in escaped_values]

    return xml_properties

  def _XmlEscapeValues(self, property):
    """ Returns a list of the XML-escaped string values for the given property.
    Raises an AssertionError if the property doesn't exist.

    Arg:
      property: string

    Returns:
      list of strings
    """
    assert self.has_key(property)
    xml = []

    values = self[property]
    if not isinstance(values, list):
      values = [values]

    for val in values:
      if hasattr(val, 'ToXml'):
        xml.append(val.ToXml())
      else:
        if val is None:
          xml.append('')
        else:
          xml.append(saxutils.escape(unicode(val)))

    return xml

  def _ToPb(self):
    """Converts this Entity to its protocol buffer representation. Not
    intended to be used by application developers.

    Returns:
      entity_pb.Entity
    """

    pb = entity_pb.EntityProto()
    pb.mutable_key().CopyFrom(self.key()._ToPb())

    group = pb.mutable_entity_group()
    if self.__key.has_id_or_name():
      root = pb.key().path().element(0)
      group.add_element().CopyFrom(root)

    properties = self.items()
    properties.sort()
    for (name, values) in properties:
      properties = datastore_types.ToPropertyPb(name, values)
      if not isinstance(properties, list):
        properties = [properties]

      sample = values
      if isinstance(sample, list):
        sample = values[0]

      if isinstance(sample, (datastore_types.Blob, datastore_types.Text)):
        pb.raw_property_list().extend(properties)
      else:
        pb.property_list().extend(properties)

    return pb

  @staticmethod
  def _FromPb(pb):
    """Static factory method. Returns the Entity representation of the
    given protocol buffer (datastore_pb.Entity). Not intended to be used by
    application developers.

    The Entity PB's key must be complete. If it isn't, an AssertionError is
    raised.

    Args:
      # a protocol buffer Entity
      pb: datastore_pb.Entity

    Returns:
      # the Entity representation of the argument
      Entity
    """
    assert pb.key().path().element_size() > 0

    last_path = pb.key().path().element_list()[-1]
    assert last_path.has_id() ^ last_path.has_name()
    if last_path.has_id():
      assert last_path.id() != 0
    else:
      assert last_path.has_name()
      assert last_path.name()
    e = Entity(unicode(last_path.type().decode('utf-8')))

    ref = e.__key._Key__reference
    ref.CopyFrom(pb.key())

    for prop_list in [pb.property_list(), pb.raw_property_list()]:
      for prop in prop_list:
        value = datastore_types.FromPropertyPb(prop)

        if not prop.has_multiple():
          raise datastore_errors.Error(
            "Property %s is corrupt in the datastore; it's missing the "
            'multiply valued field.' % name)

        if prop.multiple():
          value = [value]
        name = unicode(prop.name().decode('utf-8'))

        if not e.has_key(name):
          e[name] = value
        else:
          if not prop.multiple():
            raise datastore_errors.Error(
              'Property %s is corrupt in the datastore; it has multiple '
              'values, but is not marked as multiply valued.' % name)

          cur_value = e[name]
          assert isinstance(cur_value, list)
          cur_value += value

    return e


class Query(dict):
  """A datastore query.

  (Instead of this, consider using appengine.ext.gql.Query! It provides a
  query language interface on top of the same functionality.)

  Queries are used to retrieve entities that match certain criteria, including
  app id, kind, and property filters. Results may also be sorted by properties.

  App id and kind are required. Only entities from the given app, of the given
  type, are returned. If an ancestor is set, with Ancestor(), only entities
  with that ancestor are returned.

  Property filters are used to provide criteria based on individual property
  values. A filter compares a specific property in each entity to a given
  value or list of possible values.

  An entity is returned if its property values match *all* of the query's
  filters. In other words, filters are combined with AND, not OR. If an
  entity does not have a value for a property used in a filter, it is not
  returned.

  Property filters map filter strings of the form '<property name> <operator>'
  to filter values. Use dictionary accessors to set property filters, like so:

  > query = Query('Person')
  > query['name in'] = ['Ryan', 'Ken', 'Bret']
  > query['age >='] = 21

  This query returns all Person entities where the name property is 'Ryan',
  'Ken', or 'Bret', and the age property is at least 21.

  Another way to build this query is:

  > query = Query('Person')
  > query.update({'name in': ['Ryan', 'Ken', 'Bret'], 'age >=': 21})

  The supported operators are =, >, <, >=, and <=. Only one inequality
  filter may be used per query. Any number of equals filters may be used in
  a single Query.

  Result entities can be returned in different orders. Use the Order()
  method to specify properties that results will be sorted by, and in which
  direction.

  Note that filters and orderings may be provided at any time before the query
  is run. When the query is fully specified, Run() runs the query and returns
  an iterator. The query results can be accessed through the iterator.

  A query object may be reused after it's been run. Its filters and
  orderings can be changed to create a modified query.

  If you know how many result entities you need, use Get() to fetch them:

  > query = Query('Person', {'age >': 21})
  > for person in query.Get(4):
  >   print 'I have four pints left. Have one on me, %s!' % person['name']

  If you don't know how many results you need, or if you need them all, you
  can get an iterator over the results by calling Run():

  > for person in Query('Person', {'age >': 21}).Run():
  >   print 'Have a pint on me, %s!' % person['name']

  Get() is more efficient than Run(), so use Get() whenever possible.

  Finally, the Count() method returns the number of result entities matched by
  the query. The returned count is cached; successive Count() calls will not
  re-scan the datastore unless the query is changed.
  """
  ASCENDING = datastore_pb.Query_Order.ASCENDING
  DESCENDING = datastore_pb.Query_Order.DESCENDING

  ORDER_FIRST = datastore_pb.Query.ORDER_FIRST
  ANCESTOR_FIRST = datastore_pb.Query.ANCESTOR_FIRST
  FILTER_FIRST = datastore_pb.Query.FILTER_FIRST

  OPERATORS = {'<':  datastore_pb.Query_Filter.LESS_THAN,
               '<=': datastore_pb.Query_Filter.LESS_THAN_OR_EQUAL,
               '>':  datastore_pb.Query_Filter.GREATER_THAN,
               '>=': datastore_pb.Query_Filter.GREATER_THAN_OR_EQUAL,
               '=':  datastore_pb.Query_Filter.EQUAL,
               '==': datastore_pb.Query_Filter.EQUAL,
               }
  INEQUALITY_OPERATORS = frozenset(['<', '<=', '>', '>='])
  FILTER_REGEX = re.compile(
    '^\s*([^\s]+)(\s+(%s)\s*)?$' % '|'.join(OPERATORS.keys()),
    re.IGNORECASE | re.UNICODE)

  __kind = None
  __app = _LOCAL_APP_ID
  __orderings = None
  __cached_count = None
  __hint = None
  __ancestor = None

  __filter_order = None
  __filter_counter = 0

  __inequality_prop = None
  __inequality_count = 0

  def __init__(self, kind, filters={}, _app=None):
    """Constructor.

    Raises BadArgumentError if kind is not a string. Raises BadValueError or
    BadFilterError if filters is not a dictionary of valid filters.

    Args:
      # kind is required. filters is optional; if provided, it's used
      # as an initial set of property filters.
      kind: string
      filters: dict
    """
    datastore_types.ValidateString(kind, 'kind',
                                   datastore_errors.BadArgumentError)

    self.__kind = kind
    self.__orderings = []
    self.__filter_order = {}
    self.update(filters)

    if _app is not None:
      datastore_types.ValidateString(_app, '_app',
                                     datastore_errors.BadArgumentError)
      self.__app = _app

  def Order(self, *orderings):
    """Specify how the query results should be sorted.

    Result entities will be sorted by the first property argument, then by the
    second, and so on. For example, this:

    > query = Query('Person')
    > query.Order('bday', ('age', Query.DESCENDING))

    sorts everyone in order of their birthday, starting with January 1.
    People with the same birthday are sorted by age, oldest to youngest.

    The direction for each sort property may be provided; if omitted, it
    defaults to ascending.

    Order() may be called multiple times. Each call resets the sort order
    from scratch.

    If an inequality filter exists in this Query it must be the first property
    passed to Order. Any number of sort orders may be used after the
    inequality filter property. Without inequality filters, any number of
    filters with different orders may be specified.

    Entities with multiple values for an order property are sorted by their
    lowest value.

    Note that a sort order implies an existence filter! In other words,
    Entities without the sort order property are filtered out, and *not*
    included in the query results.

    If the sort order property has different types in different entities - ie,
    if bob['id'] is an int and fred['id'] is a string - the entities will be
    grouped first by the property type, then sorted within type. No attempt is
    made to compare property values across types.

    Raises BadArgumentError if any argument is of the wrong format.

    Args:
      # the properties to sort by, in sort order. each argument may be either a
      # string or (string, direction) 2-tuple.

    Returns:
      # this query
      Query
    """
    orderings = list(orderings)

    for (order, i) in zip(orderings, range(len(orderings))):
      if not (isinstance(order, basestring) or
              (isinstance(order, tuple) and len(order) in [2, 3])):
        raise datastore_errors.BadArgumentError(
          'Order() expects strings or 2- or 3-tuples; received %s (a %s). ' %
          (order, typename(order)))

      if isinstance(order, basestring):
        order = (order,)

      datastore_types.ValidateString(order[0], 'sort order property',
                                     datastore_errors.BadArgumentError)
      property = order[0]

      direction = order[-1]
      if direction not in (Query.ASCENDING, Query.DESCENDING):
        if len(order) == 3:
          raise datastore_errors.BadArgumentError(
            'Order() expects Query.ASCENDING or DESCENDING; received %s' %
            str(direction))
        direction = Query.ASCENDING

      orderings[i] = (property, direction)

    if (orderings and self.__inequality_prop and
        orderings[0][0] != self.__inequality_prop):
      raise datastore_errors.BadArgumentError(
        'First ordering property must be the same as inequality filter '
        'property, if specified for this query; received %s, expected %s' %
        (orderings[0][0], self.__inequality_prop))

    self.__orderings = orderings
    return self

  def Hint(self, hint):
    """Sets a hint for how this query should run.

    The query hint gives us information about how best to execute your query.
    Currently, we can only do one index scan, so the query hint should be used
    to indicates which index we should scan against.

    Use FILTER_FIRST if your first filter will only match a few results. In
    this case, it will be most efficient to scan against the index for this
    property, load the results into memory, and apply the remaining filters
    and sort orders there.

    Similarly, use ANCESTOR_FIRST if the query's ancestor only has a few
    descendants. In this case, it will be most efficient to scan all entities
    below the ancestor and load them into memory first.

    Use ORDER_FIRST if the query has a sort order and the result set is large
    or you only plan to fetch the first few results. In that case, we
    shouldn't try to load all of the results into memory; instead, we should
    scan the index for this property, which is in sorted order.

    Note that hints are currently ignored in the v3 datastore!

    Arg:
      one of datastore.Query.[ORDER_FIRST, ANCESTOR_FIRST, FILTER_FIRST]

    Returns:
      # this query
      Query
    """
    if hint not in [self.ORDER_FIRST, self.ANCESTOR_FIRST, self.FILTER_FIRST]:
      raise datastore_errors.BadArgumentError(
        'Query hint must be ORDER_FIRST, ANCESTOR_FIRST, or FILTER_FIRST.')

    self.__hint = hint
    return self

  def Ancestor(self, ancestor):
    """Sets an ancestor for this query.

    This restricts the query to only return result entities that are descended
    from a given entity. In other words, all of the results will have the
    ancestor as their parent, or parent's parent, or etc.

    Raises BadArgumentError or BadKeyError if parent is not an existing Entity
    or Key in the datastore.

    Args:
      # the key must be complete
      ancestor: Entity or Key

    Returns:
      # this query
      Query
    """
    key = _GetCompleteKeyOrError(ancestor)
    self.__ancestor = datastore_pb.Reference()
    self.__ancestor.CopyFrom(key._Key__reference)
    return self

  def Run(self):
    """Runs this query.

    If a filter string is invalid, raises BadFilterError. If a filter value is
    invalid, raises BadValueError. If an IN filter is provided, and a sort
    order on another property is provided, raises BadQueryError.

    If you know in advance how many results you want, use Get() instead. It's
    more efficient.

    Returns:
      # an iterator that provides access to the query results
      Iterator
    """
    return self._Run()

  def _Run(self, limit=None):
    """Runs this query, with an optional result limit.

    Identical to Run, with the extra optional limit parameter. limit must be
    an integer >= 0.

    This is not intended to be used by application developers. Use Get()
    instead!
    """
    if _FindTransactionInStack():
      raise datastore_errors.BadRequestError(
        "Can't query inside a transaction.")

    pb = self._ToPb(limit)
    result = datastore_pb.QueryResult()
    try:
      apiproxy_stub_map.MakeSyncCall('datastore_v3', 'RunQuery', pb, result)
    except apiproxy_errors.ApplicationError, err:
      raise _ToDatastoreError(err)
    return Iterator._FromPb(result.cursor())

  def Get(self, count):
    """Fetches and returns a certain number of results from the query.

    This method fetches and returns a list of resulting entities that matched
    the query. If the query specified a sort order, entities are returned in
    that order. Otherwise, the order is undefined.

    The argument specifies the number of entities to return. If it's greater
    than the number of remaining entities, all of the remaining entities are
    returned. In that case, the length of the returned list will be smaller
    than count.

    The results are always returned as a list. If there are no results left,
    an empty list is returned.

    If you know in advance how many results you want, this method is more
    efficient than Run(), since it fetches all of the results at once. (The
    datastore backend sets the count as the limit on the underlying
    scan, which makes the scan significantly faster.)

    Args:
      # the number of entities to return
      int or long

    Returns:
      # a list of entities
      [Entity, ...]
    """
    if not isinstance(count, (int, long)) or count <= 0:
      raise datastore_errors.BadArgumentError(
        'Argument to Get must be an int greater than 0; received %s (a %s)' %
        (count, typename(count)))

    return self._Run(count)._Next(count)

  def Count(self, limit=None):
    """Returns the number of entities that this query matches. The returned
    count is cached; successive Count() calls will not re-scan the datastore
    unless the query is changed.

    Raises BadQueryError if the Query has more than one filter. Multiple
    filters aren't supported yet.

    Args:
      limit, a number. If there are more results than this, stop short and
      just return this number. Providing this argument makes the count
      operation more efficient.
    Returns:
      The number of results.
    """
    if self.__cached_count:
      return self.__cached_count

    resp = api_base_pb.Integer64Proto()
    try:
      apiproxy_stub_map.MakeSyncCall('datastore_v3', 'Count',
                                     self._ToPb(limit=limit), resp)
    except apiproxy_errors.ApplicationError, err:
      raise _ToDatastoreError(err)
    else:
      self.__cached_count = resp.value()

    return self.__cached_count

  def __iter__(self):
    raise NotImplementedError(
      'Query objects should not be used as iterators. Call Run() first.')

  def __setitem__(self, filter, value):
    """Implements the [] operator. Used to set filters.

    If the filter string is empty or not a string, raises BadFilterError. If
    the value is not a supported type, raises BadValueError.
    """
    datastore_types.ToPropertyPb(' ', value)
    match = self.__CheckFilter(filter, value)
    property = match.group(1)
    operator = match.group(3)

    dict.__setitem__(self, filter, value)

    if operator in self.INEQUALITY_OPERATORS:
      if self.__inequality_prop is None:
        self.__inequality_prop = property
      else:
        assert self.__inequality_prop == property
      self.__inequality_count += 1

    if filter not in self.__filter_order:
      self.__filter_order[filter] = self.__filter_counter
      self.__filter_counter += 1

    self.__cached_count = None

  def setdefault(self, filter, value):
    """If the filter exists, returns its value. Otherwise sets it to value.

    If the property name is the empty string or not a string, raises
    BadPropertyError. If the value is not a supported type, raises
    BadValueError.
    """
    datastore_types.ToPropertyPb(' ', value)
    self.__CheckFilter(filter, value)
    self.__cached_count = None
    return dict.setdefault(self, filter, value)

  def __delitem__(self, filter):
    """Implements the del [] operator. Used to remove filters.
    """
    dict.__delitem__(self, filter)
    del self.__filter_order[filter]
    self.__cached_count = None

    match = Query.FILTER_REGEX.match(filter)
    property = match.group(1)
    operator = match.group(3)

    if operator in self.INEQUALITY_OPERATORS:
      assert self.__inequality_count >= 1
      assert property == self.__inequality_prop
      self.__inequality_count -= 1
      if self.__inequality_count == 0:
        self.__inequality_prop = None

  def update(self, other):
    """Updates this query's filters from the ones in other.

    If any filter string is invalid, raises BadFilterError. If any value is
    not a supported type, raises BadValueError.
    """
    for filter, value in other.items():
      self.__setitem__(filter, value)

  def copy(self):
    """The copy method is not supported.
    """
    raise NotImplementedError('Query does not support the copy() method.')

  def __CheckFilter(self, filter, values):
    """Type check a filter string and list of values.

    Raises BadFilterError if the filter string is empty, not a string, or
    invalid. Raises BadValueError if the value type is not supported.

    Args:
      filter: String containing the filter text.
      values: List of associated filter values.

    Returns:
      re.MatchObject (never None) that matches the 'filter'. Group 1 is the
      property name, group 3 is the operator. (Group 2 is unused.)
    """
    try:
      match = Query.FILTER_REGEX.match(filter)
      if not match:
        raise datastore_errors.BadFilterError(
          'Could not parse filter string: %s' % str(filter))
    except TypeError:
      raise datastore_errors.BadFilterError(
        'Could not parse filter string: %s' % str(filter))

    property = match.group(1)
    operator = match.group(3)

    if isinstance(values, list) and len(values) != 1:
      raise datastore_errors.BadValueError(
          '%r requires a single value; received %r:' %
          (operator, values))

    if not isinstance(values, list):
      values = [values]
    if isinstance(values[0], datastore_types.Blob):
      raise datastore_errors.BadValueError(
        'Filtering on Blob properties is not supported.')
    if isinstance(values[0], datastore_types.Text):
      raise datastore_errors.BadValueError(
        'Filtering on Text properties is not supported.')

    if operator in self.INEQUALITY_OPERATORS:
      if isinstance(values[0], Key):
        raise datastore_errors.BadFilterError(
          'Inequality operators (%s) are not yet supported on Key properties.' %
          ', '.join(self.INEQUALITY_OPERATORS))
      elif self.__inequality_prop and property != self.__inequality_prop:
        raise datastore_errors.BadFilterError(
          'Only one property per query may have inequality filters (%s).' %
          ', '.join(self.INEQUALITY_OPERATORS))
      elif len(self.__orderings) >= 1 and self.__orderings[0][0] != property:
        raise datastore_errors.BadFilterError(
          'Inequality operators (%s) must be on the same property as the '
          'first sort order, if any sort orders are supplied' %
          ', '.join(self.INEQUALITY_OPERATORS))

    return match

  def _ToPb(self, limit=None):
    """Converts this Query to its protocol buffer representation. Not
    intended to be used by application developers. Enforced by hiding the
    datastore_pb classes.

    Args:
      # an upper bound on the number of results returned by the query.
      limit: int

    Returns:
      # the PB representation of this Query
      datastore_pb.Query
    """
    pb = datastore_pb.Query()

    pb.set_kind(self.__kind.encode('utf-8'))
    if self.__app:
      pb.set_app(self.__app.encode('utf-8'))
    if limit is not None:
      pb.set_limit(limit)
    if self.__ancestor:
      pb.mutable_ancestor().CopyFrom(self.__ancestor)

    if ((self.__hint == self.ORDER_FIRST and self.__orderings) or
        (self.__hint == self.ANCESTOR_FIRST and self.__ancestor) or
        (self.__hint == self.FILTER_FIRST and len(self) > 0)):
      pb.set_hint(self.__hint)

    ordered_filters = [(i, f) for f, i in self.__filter_order.iteritems()]
    ordered_filters.sort()

    for i, filter_str in ordered_filters:
      if filter_str not in self:
        continue
      values = self[filter_str]

      match = self.__CheckFilter(filter_str, values)
      filter = pb.add_filter()

      op = match.group(3)
      if op is None:
        op = '='
      filter.set_op(self.OPERATORS[op])

      name = match.group(1)
      props = datastore_types.ToPropertyPb(name, values)
      if not isinstance(props, list):
        props = [props]
      filter.property_list().extend(props)

    for property, direction in self.__orderings:
      order = pb.add_order()
      order.set_property(property.encode('utf-8'))
      order.set_direction(direction)

    return pb


class Iterator(object):
  """An iterator over the results of a datastore query.

  Iterators are used to access the results of a Query. An iterator is
  obtained by building a Query, then calling Run() on it.

  Iterator implements Python's iterator protocol, so results can be accessed
  with the for and in statements:

  > it = Query('Person').Run()
  > for person in it:
  >   print 'Hi, %s!' % person['name']
  """
  def __init__(self, cursor):
    self.__cursor = cursor
    self.__buffer = []
    self.__more_results = True

  def _Next(self, count):
    """Returns the next result(s) of the query.

    Not intended to be used by application developers. Use the python
    iterator protocol instead.

    This method returns the next entities from the list of resulting
    entities that matched the query. If the query specified a sort
    order, entities are returned in that order. Otherwise, the order
    is undefined.

    The argument specifies the number of entities to return. If it's
    greater than the number of remaining entities, all of the
    remaining entities are returned. In that case, the length of the
    returned list will be smaller than count.

    There is an internal buffer for use with the next() method.  If
    this buffer is not empty, up to 'count' values are removed from
    this buffer and returned.  It's best not to mix _Next() and
    next().

    The results are always returned as a list. If there are no results
    left, an empty list is returned.

    Args:
      # the number of entities to return; must be >= 1
      count: int or long

    Returns:
      # a list of entities
      [Entity, ...]
    """
    if not isinstance(count, (int, long)) or count <= 0:
      raise datastore_errors.BadArgumentError(
        'Argument to _Next must be an int greater than 0; received %s (a %s)' %
        (count, typename(count)))

    if self.__buffer:
      raise datastore_errors.BadRequestError(
          'You can\'t mix next() and _Next()')

    if not self.__more_results:
      return []

    req = datastore_pb.NextRequest()
    req.set_count(count)
    req.mutable_cursor().CopyFrom(self._ToPb())
    result = datastore_pb.QueryResult()
    try:
      apiproxy_stub_map.MakeSyncCall('datastore_v3', 'Next', req, result)
    except apiproxy_errors.ApplicationError, err:
      raise _ToDatastoreError(err)

    self.__more_results = result.more_results()

    ret = [Entity._FromPb(r) for r in result.result_list()]
    return ret

  _BUFFER_SIZE = 20

  def next(self):
    if not self.__buffer:
      self.__buffer = self._Next(self._BUFFER_SIZE)
    try:
      return self.__buffer.pop(0)
    except IndexError:
      raise StopIteration

  def __iter__(self): return self

  def _ToPb(self):
    """Converts this Iterator to its protocol buffer representation. Not
    intended to be used by application developers. Enforced by hiding the
    datastore_pb classes.

    Returns:
      # the PB representation of this Iterator
      datastore_pb.Cursor
    """
    pb = datastore_pb.Cursor()
    pb.set_cursor(self.__cursor)
    return pb

  @staticmethod
  def _FromPb(pb):
    """Static factory method. Returns the Iterator representation of the given
    protocol buffer (datastore_pb.Cursor). Not intended to be used by
    application developers. Enforced by not hiding the datastore_pb classes.

    Args:
      # a protocol buffer Cursor
      pb: datastore_pb.Cursor

    Returns:
      # the Iterator representation of the argument
      Iterator
    """
    return Iterator(pb.cursor())


def RunInTransaction(function, *args, **kwargs):
  """Runs a function inside a datastore transaction.

  Runs the user-provided function inside a full-featured, ACID datastore
  transaction. Every Put, Get, and Delete call in the function is made within
  the transaction. All entities involved in these calls must belong to the
  same entity group. Queries are not supported.

  The trailing arguments are passed to the function as positional arguments.
  If the function returns a value, that value will be returned by
  RunInTransaction. Otherwise, it will return None.

  The function may raise any exception to roll back the transaction instead of
  committing it. If this happens, the transaction will be rolled back and the
  exception will be re-raised up to RunInTransaction's caller.

  If you want to roll back intentionally, but don't have an appropriate
  exception to raise, you can raise an instance of datastore_errors.Rollback.
  It will cause a rollback, but will *not* be re-raised up to the caller.

  The function may be run more than once, so it should be idempotent. It
  should avoid side effects, and it shouldn't have *any* side effects that
  aren't safe to occur multiple times. This includes modifying the arguments,
  since they persist across invocations of the function. However, this doesn't
  include Put, Get, and Delete calls, of course.

  Example usage:

  > def decrement(key, amount=1):
  >   counter = datastore.Get(key)
  >   counter['count'] -= amount
  >   if counter['count'] < 0:    # don't let the counter go negative
  >     raise datastore_errors.Rollback()
  >   datastore.Put(counter)
  >
  > counter = datastore.Query('Counter', {'name': 'foo'})
  > datastore.RunInTransaction(decrement, counter.key(), amount=5)

  Transactions satisfy the traditional ACID properties. They are:

  - Atomic. All of a transaction's operations are executed or none of them are.

  - Consistent. The datastore's state is consistent before and after a
  transaction, whether it committed or rolled back. Invariants such as
  "every entity has a primary key" are preserved.

  - Isolated. Transactions operate on a snapshot of the datastore. Other
  datastore operations do not see intermediated effects of the transaction;
  they only see its effects after it has committed.

  - Durable. On commit, all writes are persisted to the datastore.

  Nested transactions are not yet supported.

  Args:
    # a function to be run inside the transaction
    function: callable
    # positional arguments to pass to the function
    args: variable number of any type

  Returns:
    the function's return value, if any

  Raises:
    TransactionFailedError, if the transaction could not be committed.
  """
  frame = None

  try:
    if _FindTransactionInStack():
      raise datastore_errors.BadRequestError(
        'Nested transactions are not yet supported.')

    frame = sys._getframe()
    for i in range(0, TRANSACTION_RETRIES + 1):
      try:
        result = function(*args, **kwargs)
      except:
        original_exception = sys.exc_info()

        if frame in _txes:
          try:
            resp = api_base_pb.VoidProto()
            apiproxy_stub_map.MakeSyncCall('datastore_v3', 'Rollback',
                                           _txes[frame], resp)
          except:
            exc_info = sys.exc_info()
            logging.info('Exception sending Rollback:\n' +
                         ''.join(traceback.format_exception(*exc_info)))

        type, value, trace = original_exception
        if type is datastore_errors.Rollback:
          return
        else:
          raise type, value, trace

      if frame in _txes:
        try:
          try:
            resp = api_base_pb.VoidProto()
            apiproxy_stub_map.MakeSyncCall('datastore_v3', 'Commit',
                                           _txes[frame], resp)
          except apiproxy_errors.ApplicationError, err:
            if (err.application_error ==
                datastore_pb.Error.CONCURRENT_TRANSACTION):
              logging.warning('Transaction collision for entity group with '
                              'key %r', _tx_entity_groups[frame])
              continue
        finally:
          del _txes[frame]
          del _tx_entity_groups[frame]

      return result

    raise datastore_errors.TransactionFailedError(
      'The transaction could not be committed. Please try again.')

  finally:
    if frame in _txes:
      del _txes[frame]
      del _tx_entity_groups[frame]
    del frame


def _MaybeSetupTransaction(request, key_or_entity):
  """Begins a transaction, and populates it in the request, if necessary.

  If we're currently inside a transaction, this records the entity group,
  creates the transaction PB, and sends the BeginTransaction. It then
  populates the transaction handle in the request.

  Raises BadRequestError if the entity has a different entity group than the
  current transaction.

  Args:
    request: GetRequest, PutRequest, or DeleteRequest
    key_or_entity: Key or Entity
  """
  assert isinstance(request, (datastore_pb.GetRequest, datastore_pb.PutRequest,
                              datastore_pb.DeleteRequest))
  assert isinstance(key_or_entity, (Key, Entity))
  frame = None

  try:
    frame = _FindTransactionInStack()
    if frame:
      this_group = key_or_entity._entity_group()
      if frame not in _txes:
        _txes[frame] = datastore_pb.Transaction()
        _tx_entity_groups[frame] = this_group
        req = api_base_pb.VoidProto()
        apiproxy_stub_map.MakeSyncCall('datastore_v3', 'BeginTransaction', req,
                                       _txes[frame])
      else:
        orig_group = _tx_entity_groups[frame]
        if orig_group != this_group:
          def id_or_name(key):
            if (key.name()):
              return 'name=%r' % key.name()
            else:
              return 'id=%r' % key.id()
          raise datastore_errors.BadRequestError(
            'Cannot operate on different entity groups in a transaction: '
            '(kind=%r, %s) and (kind=%r, %s).' %
            (orig_group.kind(), id_or_name(orig_group),
             this_group.kind(), id_or_name(this_group)))

      request.mutable_transaction().CopyFrom(_txes[frame])

  finally:
    del frame


def _FindTransactionInStack():
  """Walks the stack to find a RunInTransaction() call.

  Returns:
    # this is the RunInTransaction() frame record, if found
    frame record or None
  """
  frame = sys._getframe()
  filename = frame.f_code.co_filename

  frame = frame.f_back.f_back
  while frame:
    if (frame.f_code.co_filename == filename and
        frame.f_code.co_name == 'RunInTransaction'):
      return frame
    frame = frame.f_back

  return None


def _GetCompleteKeyOrError(arg):
  """Expects an Entity or a Key, and returns the corresponding Key. Raises
  BadArgumentError or BadKeyError if arg is a different type or is incomplete.

  Args:
    arg: Entity or Key

  Returns:
    Key
  """
  if isinstance(arg, Key):
    key = arg
  elif isinstance(arg, basestring):
    key = Key(arg)
  elif isinstance(arg, Entity):
    key = arg.key()
  elif not isinstance(arg, Key):
    raise datastore_errors.BadArgumentError(
      'Expects argument to be an Entity or Key; received %s (a %s).' %
      (arg, typename(arg)))
  assert isinstance(key, Key)

  if not key.has_id_or_name():
    raise datastore_errors.BadKeyError('Key %r is not complete.' % key)

  return key


def _ToDatastoreError(err):
  """Converts an apiproxy.ApplicationError to an error in datastore_errors.

  Args:
    err: apiproxy.ApplicationError

  Returns:
    a subclass of datastore_errors.Error
  """
  errors = {
    datastore_pb.Error.BAD_REQUEST: datastore_errors.BadRequestError,
    datastore_pb.Error.CONCURRENT_TRANSACTION:
      datastore_errors.TransactionFailedError,
    datastore_pb.Error.INTERNAL_ERROR: datastore_errors.InternalError,
    datastore_pb.Error.NEED_INDEX: datastore_errors.NeedIndexError,
    datastore_pb.Error.TIMEOUT: datastore_errors.Timeout,
    }

  if err.application_error in errors:
    raise errors[err.application_error](err.error_detail)
  else:
    raise datastore_errors.Error(err.error_detail)
