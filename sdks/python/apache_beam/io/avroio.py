#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""``PTransforms`` for reading from and writing to Avro files.

Provides two read ``PTransform``s, ``ReadFromAvro`` and ``ReadAllFromAvro``,
that produces a ``PCollection`` of records.
Each record of this ``PCollection`` will contain a single record read from
an Avro file. Records that are of simple types will be mapped into
corresponding Python types. Records that are of Avro type 'RECORD' will be
mapped to Python dictionaries that comply with the schema contained in the
Avro file that contains those records. In this case, keys of each dictionary
will contain the corresponding field names and will be of type ``string``
while the values of the dictionary will be of the type defined in the
corresponding Avro schema.

For example, if schema of the Avro file is the following.
{"namespace": "example.avro","type": "record","name": "User","fields":
[{"name": "name", "type": "string"},
{"name": "favorite_number",  "type": ["int", "null"]},
{"name": "favorite_color", "type": ["string", "null"]}]}

Then records generated by read transforms will be dictionaries of the
following form.
{'name': 'Alyssa', 'favorite_number': 256, 'favorite_color': None}).

Additionally, this module provides a write ``PTransform`` ``WriteToAvro``
that can be used to write a given ``PCollection`` of Python objects to an
Avro file.
"""
# pytype: skip-file
import ctypes
import os
from functools import partial
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Union

import fastavro
from fastavro.read import block_reader
from fastavro.write import Writer

import apache_beam as beam
from apache_beam.io import filebasedsink
from apache_beam.io import filebasedsource
from apache_beam.io import iobase
from apache_beam.io.filesystem import CompressionTypes
from apache_beam.io.filesystems import FileSystems
from apache_beam.io.iobase import Read
from apache_beam.portability.api import schema_pb2
from apache_beam.transforms import PTransform
from apache_beam.typehints import schemas

__all__ = [
    'ReadFromAvro',
    'ReadAllFromAvro',
    'ReadAllFromAvroContinuously',
    'WriteToAvro'
]


class ReadFromAvro(PTransform):
  """A `PTransform` for reading records from avro files.

  Each record of the resulting PCollection will contain
  a single record read from a source. Records that are of simple types will be
  mapped to beam Rows with a single `record` field containing the records
  value. Records that are of Avro type ``RECORD`` will be mapped to Beam rows
  that comply with the schema contained in the Avro file that contains those
  records.
  """
  def __init__(
      self,
      file_pattern=None,
      min_bundle_size=0,
      validate=True,
      use_fastavro=True,
      as_rows=False):
    """Initializes :class:`ReadFromAvro`.

    Uses source :class:`~apache_beam.io._AvroSource` to read a set of Avro
    files defined by a given file pattern.

    If ``/mypath/myavrofiles*`` is a file-pattern that points to a set of Avro
    files, a :class:`~apache_beam.pvalue.PCollection` for the records in
    these Avro files can be created in the following manner.

    .. testcode::

      with beam.Pipeline() as p:
        records = p | 'Read' >> beam.io.ReadFromAvro('/mypath/myavrofiles*')

    .. NOTE: We're not actually interested in this error; but if we get here,
       it means that the way of calling this transform hasn't changed.

    .. testoutput::
      :hide:

      Traceback (most recent call last):
       ...
      OSError: No files found based on the file pattern

    Each record of this :class:`~apache_beam.pvalue.PCollection` will contain
    a single record read from a source. Records that are of simple types will be
    mapped into corresponding Python types. Records that are of Avro type
    ``RECORD`` will be mapped to Python dictionaries that comply with the schema
    contained in the Avro file that contains those records. In this case, keys
    of each dictionary will contain the corresponding field names and will be of
    type :class:`str` while the values of the dictionary will be of the type
    defined in the corresponding Avro schema.

    For example, if schema of the Avro file is the following. ::

      {
        "namespace": "example.avro",
        "type": "record",
        "name": "User",
        "fields": [

          {"name": "name",
           "type": "string"},

          {"name": "favorite_number",
           "type": ["int", "null"]},

          {"name": "favorite_color",
           "type": ["string", "null"]}

        ]
      }

    Then records generated by :class:`~apache_beam.io._AvroSource` will be
    dictionaries of the following form. ::

      {'name': 'Alyssa', 'favorite_number': 256, 'favorite_color': None}).

    Args:
      file_pattern (str): the file glob to read
      min_bundle_size (int): the minimum size in bytes, to be considered when
        splitting the input into bundles.
      validate (bool): flag to verify that the files exist during the pipeline
        creation time.
      use_fastavro (bool): This flag is left for API backwards compatibility
        and no longer has an effect.  Do not use.
      as_rows (bool): Whether to return a schema'd PCollection of Beam rows.
    """
    super().__init__()
    self._source = _FastAvroSource(
        file_pattern, min_bundle_size, validate=validate)
    if as_rows:
      path = FileSystems.match([file_pattern], [1])[0].metadata_list[0].path
      with FileSystems.open(path) as fin:
        avro_schema = fastavro.reader(fin).writer_schema
        beam_schema = avro_schema_to_beam_schema(avro_schema)
      self._post_process = avro_dict_to_beam_row(avro_schema, beam_schema)
    else:
      self._post_process = None

  def expand(self, pvalue):
    records = pvalue.pipeline | Read(self._source)
    if self._post_process:
      return records | beam.Map(self._post_process)
    else:
      return records

  def display_data(self):
    return {'source_dd': self._source}


class ReadAllFromAvro(PTransform):
  """A ``PTransform`` for reading ``PCollection`` of Avro files.

  Uses source '_AvroSource' to read a ``PCollection`` of Avro files or file
  patterns and produce a ``PCollection`` of Avro records.

  This implementation is only tested with batch pipeline. In streaming,
  reading may happen with delay due to the limitation in ReShuffle involved.
  """

  DEFAULT_DESIRED_BUNDLE_SIZE = 64 * 1024 * 1024  # 64MB

  def __init__(
      self,
      min_bundle_size=0,
      desired_bundle_size=DEFAULT_DESIRED_BUNDLE_SIZE,
      use_fastavro=True,
      with_filename=False,
      label='ReadAllFiles'):
    """Initializes ``ReadAllFromAvro``.

    Args:
      min_bundle_size: the minimum size in bytes, to be considered when
                       splitting the input into bundles.
      desired_bundle_size: the desired size in bytes, to be considered when
                       splitting the input into bundles.
      use_fastavro (bool): This flag is left for API backwards compatibility
        and no longer has an effect. Do not use.
      with_filename: If True, returns a Key Value with the key being the file
        name and the value being the actual data. If False, it only returns
        the data.
    """
    source_from_file = partial(_FastAvroSource, min_bundle_size=min_bundle_size)
    self._read_all_files = filebasedsource.ReadAllFiles(
        True,
        CompressionTypes.AUTO,
        desired_bundle_size,
        min_bundle_size,
        source_from_file,
        with_filename)

    self.label = label

  def expand(self, pvalue):
    return pvalue | self.label >> self._read_all_files


class ReadAllFromAvroContinuously(ReadAllFromAvro):
  """A ``PTransform`` for reading avro files in given file patterns.
  This PTransform acts as a Source and produces continuously a ``PCollection``
  of Avro records.

  For more details, see ``ReadAllFromAvro`` for avro parsing settings;
  see ``apache_beam.io.fileio.MatchContinuously`` for watching settings.

  ReadAllFromAvroContinuously is experimental.  No backwards-compatibility
  guarantees. Due to the limitation on Reshuffle, current implementation does
  not scale.
  """
  _ARGS_FOR_MATCH = (
      'interval',
      'has_deduplication',
      'start_timestamp',
      'stop_timestamp',
      'match_updated_files',
      'apply_windowing')
  _ARGS_FOR_READ = (
      'min_bundle_size', 'desired_bundle_size', 'use_fastavro', 'with_filename')

  def __init__(self, file_pattern, label='ReadAllFilesContinuously', **kwargs):
    """Initialize the ``ReadAllFromAvroContinuously`` transform.

    Accepts args for constructor args of both :class:`ReadAllFromAvro` and
    :class:`~apache_beam.io.fileio.MatchContinuously`.
    """
    kwargs_for_match = {
        k: v
        for (k, v) in kwargs.items() if k in self._ARGS_FOR_MATCH
    }
    kwargs_for_read = {
        k: v
        for (k, v) in kwargs.items() if k in self._ARGS_FOR_READ
    }
    kwargs_additinal = {
        k: v
        for (k, v) in kwargs.items()
        if k not in self._ARGS_FOR_MATCH and k not in self._ARGS_FOR_READ
    }
    super().__init__(label=label, **kwargs_for_read, **kwargs_additinal)
    self._file_pattern = file_pattern
    self._kwargs_for_match = kwargs_for_match

  def expand(self, pbegin):
    # Importing locally to prevent circular dependency issues.
    from apache_beam.io.fileio import MatchContinuously

    # TODO(BEAM-14497) always reshuffle once gbk always trigger works.
    return (
        pbegin
        | MatchContinuously(self._file_pattern, **self._kwargs_for_match)
        | 'ReadAllFiles' >> self._read_all_files._disable_reshuffle())


class _AvroUtils(object):
  @staticmethod
  def advance_file_past_next_sync_marker(f, sync_marker):
    buf_size = 10000

    data = f.read(buf_size)
    while data:
      pos = data.find(sync_marker)
      if pos >= 0:
        # Adjusting the current position to the ending position of the sync
        # marker.
        backtrack = len(data) - pos - len(sync_marker)
        f.seek(-1 * backtrack, os.SEEK_CUR)
        return True
      else:
        if f.tell() >= len(sync_marker):
          # Backtracking in case we partially read the sync marker during the
          # previous read. We only have to backtrack if there are at least
          # len(sync_marker) bytes before current position. We only have to
          # backtrack (len(sync_marker) - 1) bytes.
          f.seek(-1 * (len(sync_marker) - 1), os.SEEK_CUR)
        data = f.read(buf_size)


class _FastAvroSource(filebasedsource.FileBasedSource):
  """A source for reading Avro files using the `fastavro` library.

  ``_FastAvroSource`` is implemented using the file-based source framework
  available in module 'filebasedsource'. Hence please refer to module
  'filebasedsource' to fully understand how this source implements operations
  common to all file-based sources such as file-pattern expansion and splitting
  into bundles for parallel processing.

  TODO: remove ``_AvroSource`` in favor of using ``_FastAvroSource``
  everywhere once it has been more widely tested
  """
  def read_records(self, file_name, range_tracker):
    next_block_start = -1

    def split_points_unclaimed(stop_position):
      if next_block_start >= stop_position:
        # Next block starts at or after the suggested stop position. Hence
        # there will not be split points to be claimed for the range ending at
        # suggested stop position.
        return 0

      return iobase.RangeTracker.SPLIT_POINTS_UNKNOWN

    range_tracker.set_split_points_unclaimed_callback(split_points_unclaimed)

    start_offset = range_tracker.start_position()
    if start_offset is None:
      start_offset = 0

    with self.open_file(file_name) as f:
      blocks = block_reader(f)
      sync_marker = blocks._header['sync']

      # We have to start at current position if previous bundle ended at the
      # end of a sync marker.
      start_offset = max(0, start_offset - len(sync_marker))
      f.seek(start_offset)
      _AvroUtils.advance_file_past_next_sync_marker(f, sync_marker)

      next_block_start = f.tell()

      while range_tracker.try_claim(next_block_start):
        block = next(blocks)
        next_block_start = block.offset + block.size
        for record in block:
          yield record


_create_avro_source = _FastAvroSource


class WriteToAvro(beam.transforms.PTransform):
  """A ``PTransform`` for writing avro files.

  If the input has a schema, a corresponding avro schema will be automatically
  generated and used to write the output records."""
  def __init__(
      self,
      file_path_prefix,
      schema=None,
      codec='deflate',
      file_name_suffix='',
      num_shards=0,
      shard_name_template=None,
      mime_type='application/x-avro',
      use_fastavro=True):
    """Initialize a WriteToAvro transform.

    Args:
      file_path_prefix: The file path to write to. The files written will begin
        with this prefix, followed by a shard identifier (see num_shards), and
        end in a common extension, if given by file_name_suffix. In most cases,
        only this argument is specified and num_shards, shard_name_template, and
        file_name_suffix use default values.
      schema: The schema to use (dict).
      codec: The codec to use for block-level compression. Any string supported
        by the Avro specification is accepted (for example 'null').
      file_name_suffix: Suffix for the files written.
      num_shards: The number of files (shards) used for output. If not set, the
        service will decide on the optimal number of shards.
        Constraining the number of shards is likely to reduce
        the performance of a pipeline.  Setting this value is not recommended
        unless you require a specific number of output files.
      shard_name_template: A template string containing placeholders for
        the shard number and shard count. When constructing a filename for a
        particular shard number, the upper-case letters 'S' and 'N' are
        replaced with the 0-padded shard number and shard count respectively.
        This argument can be '' in which case it behaves as if num_shards was
        set to 1 and only one file will be generated. The default pattern used
        is '-SSSSS-of-NNNNN' if None is passed as the shard_name_template.
      mime_type: The MIME type to use for the produced files, if the filesystem
        supports specifying MIME types.
      use_fastavro (bool): This flag is left for API backwards compatibility
        and no longer has an effect. Do not use.

    Returns:
      A WriteToAvro transform usable for writing.
    """
    self._schema = schema
    self._sink_provider = lambda avro_schema: _create_avro_sink(
        file_path_prefix,
        avro_schema,
        codec,
        file_name_suffix,
        num_shards,
        shard_name_template,
        mime_type)

  def expand(self, pcoll):
    if self._schema:
      avro_schema = self._schema
      records = pcoll
    else:
      try:
        beam_schema = schemas.schema_from_element_type(pcoll.element_type)
      except TypeError as exn:
        raise ValueError(
            "An explicit schema is required to write non-schema'd PCollections."
        ) from exn
      avro_schema = beam_schema_to_avro_schema(beam_schema)
      records = pcoll | beam.Map(
          beam_row_to_avro_dict(avro_schema, beam_schema))
    self._sink = self._sink_provider(avro_schema)
    return records | beam.io.iobase.Write(self._sink)

  def display_data(self):
    return {'sink_dd': self._sink}


def _create_avro_sink(
    file_path_prefix,
    schema,
    codec,
    file_name_suffix,
    num_shards,
    shard_name_template,
    mime_type):
  if "class 'avro.schema" in str(type(schema)):
    raise ValueError(
        'You are using Avro IO with fastavro (default with Beam on '
        'Python 3), but supplying a schema parsed by avro-python3. '
        'Please change the schema to a dict.')
  return _FastAvroSink(
      file_path_prefix,
      schema,
      codec,
      file_name_suffix,
      num_shards,
      shard_name_template,
      mime_type)


class _BaseAvroSink(filebasedsink.FileBasedSink):
  """A base for a sink for avro files. """
  def __init__(
      self,
      file_path_prefix,
      schema,
      codec,
      file_name_suffix,
      num_shards,
      shard_name_template,
      mime_type):
    super().__init__(
        file_path_prefix,
        file_name_suffix=file_name_suffix,
        num_shards=num_shards,
        shard_name_template=shard_name_template,
        coder=None,
        mime_type=mime_type,
        # Compression happens at the block level using the supplied codec, and
        # not at the file level.
        compression_type=CompressionTypes.UNCOMPRESSED)
    self._schema = schema
    self._codec = codec

  def display_data(self):
    res = super().display_data()
    res['codec'] = str(self._codec)
    res['schema'] = str(self._schema)
    return res


class _FastAvroSink(_BaseAvroSink):
  """A sink for avro files using FastAvro. """
  def __init__(
      self,
      file_path_prefix,
      schema,
      codec,
      file_name_suffix,
      num_shards,
      shard_name_template,
      mime_type):
    super().__init__(
        file_path_prefix,
        schema,
        codec,
        file_name_suffix,
        num_shards,
        shard_name_template,
        mime_type)
    self.file_handle = None

  def open(self, temp_path):
    self.file_handle = super().open(temp_path)
    return Writer(self.file_handle, self._schema, self._codec)

  def write_record(self, writer, value):
    writer.write(value)

  def close(self, writer):
    writer.flush()
    self.file_handle.close()


AVRO_PRIMITIVES_TO_BEAM_PRIMITIVES = {
    'boolean': schema_pb2.BOOLEAN,
    'int': schema_pb2.INT32,
    'long': schema_pb2.INT64,
    'float': schema_pb2.FLOAT,
    'double': schema_pb2.DOUBLE,
    'bytes': schema_pb2.BYTES,
    'string': schema_pb2.STRING,
}

BEAM_PRIMITIVES_TO_AVRO_PRIMITIVES = {
    v: k
    for k, v in AVRO_PRIMITIVES_TO_BEAM_PRIMITIVES.items()
}

_AvroSchemaType = Union[str, List, Dict]


def avro_union_type_to_beam_type(union_type: List) -> schema_pb2.FieldType:
  """convert an avro union type to a beam type

  if the union type is a nullable, and it is a nullable union of an avro
  primitive with a corresponding beam primitive then create a nullable beam
  field of the corresponding beam type, otherwise return an Any type.
  """
  if len(union_type) == 2 and "null" in union_type:
    for avro_type in union_type:
      if avro_type in AVRO_PRIMITIVES_TO_BEAM_PRIMITIVES:
        return schema_pb2.FieldType(
            atomic_type=AVRO_PRIMITIVES_TO_BEAM_PRIMITIVES[avro_type],
            nullable=True)
    return schemas.typing_to_runner_api(Any)
  return schemas.typing_to_runner_api(Any)


def avro_type_to_beam_type(avro_type: _AvroSchemaType) -> schema_pb2.FieldType:
  if isinstance(avro_type, str):
    return avro_type_to_beam_type({'type': avro_type})
  elif isinstance(avro_type, list):
    # Union type
    return avro_union_type_to_beam_type(avro_type)
  type_name = avro_type['type']
  if type_name in AVRO_PRIMITIVES_TO_BEAM_PRIMITIVES:
    return schema_pb2.FieldType(
        atomic_type=AVRO_PRIMITIVES_TO_BEAM_PRIMITIVES[type_name])
  elif type_name in ('fixed', 'enum'):
    return schema_pb2.FieldType(atomic_type=schema_pb2.STRING)
  elif type_name == 'array':
    return schema_pb2.FieldType(
        array_type=schema_pb2.ArrayType(
            element_type=avro_type_to_beam_type(avro_type['items'])))
  elif type_name == 'map':
    return schema_pb2.FieldType(
        map_type=schema_pb2.MapType(
            key_type=schema_pb2.FieldType(atomic_type=schema_pb2.STRING),
            value_type=avro_type_to_beam_type(avro_type['values'])))
  elif type_name == 'record':
    return schema_pb2.FieldType(
        row_type=schema_pb2.RowType(
            schema=schema_pb2.Schema(
                fields=[
                    schemas.schema_field(
                        f['name'], avro_type_to_beam_type(f['type']))
                    for f in avro_type['fields']
                ])))
  else:
    raise ValueError(f'Unable to convert {avro_type} to a Beam schema.')


def avro_schema_to_beam_schema(
    avro_schema: _AvroSchemaType) -> schema_pb2.Schema:
  beam_type = avro_type_to_beam_type(avro_schema)
  if isinstance(avro_schema, dict) and avro_schema['type'] == 'record':
    return beam_type.row_type.schema
  else:
    return schema_pb2.Schema(fields=[schemas.schema_field('record', beam_type)])


def avro_dict_to_beam_row(
    avro_schema: _AvroSchemaType,
    beam_schema: schema_pb2.Schema) -> Callable[[Any], Any]:
  if isinstance(avro_schema, str):
    return avro_dict_to_beam_row({'type': avro_schema})
  if avro_schema['type'] == 'record':
    to_row = avro_value_to_beam_value(
        schema_pb2.FieldType(row_type=schema_pb2.RowType(schema=beam_schema)))
  else:

    def to_row(record):
      return beam.Row(record=record)

  return beam.typehints.with_output_types(
      schemas.named_tuple_from_schema(beam_schema))(
          to_row)


def avro_atomic_value_to_beam_atomic_value(avro_type: str, value):
  """convert an avro atomic value to a beam atomic value

  if the avro type is an int or long, convert the value into from signed to
  unsigned because VarInt.java expects the number to be unsigned when
  decoding the number.
  """
  if avro_type == "int":
    return ctypes.c_uint32(value).value
  elif avro_type == "long":
    return ctypes.c_uint64(value).value
  else:
    return value


def avro_value_to_beam_value(
    beam_type: schema_pb2.FieldType) -> Callable[[Any], Any]:
  type_info = beam_type.WhichOneof("type_info")
  if type_info == "atomic_type":
    avro_type = BEAM_PRIMITIVES_TO_AVRO_PRIMITIVES[beam_type.atomic_type]
    return lambda value: avro_atomic_value_to_beam_atomic_value(
        avro_type, value)
  elif type_info == "array_type":
    element_converter = avro_value_to_beam_value(
        beam_type.array_type.element_type)
    return lambda value: [element_converter(e) for e in value]
  elif type_info == "iterable_type":
    element_converter = avro_value_to_beam_value(
        beam_type.iterable_type.element_type)
    return lambda value: [element_converter(e) for e in value]
  elif type_info == "map_type":
    if beam_type.map_type.key_type.atomic_type != schema_pb2.STRING:
      raise TypeError(
          f'Only strings allowd as map keys when converting from AVRO, '
          f'found {beam_type}')
    value_converter = avro_value_to_beam_value(beam_type.map_type.value_type)
    return lambda value: {k: value_converter(v) for (k, v) in value.items()}
  elif type_info == "row_type":
    converters = {
        field.name: avro_value_to_beam_value(field.type)
        for field in beam_type.row_type.schema.fields
    }
    return lambda value: beam.Row(
        **
        {name: convert(value[name])
         for (name, convert) in converters.items()})
  elif type_info == "logical_type":
    return lambda value: value
  else:
    raise ValueError(f"Unrecognized type_info: {type_info!r}")


def beam_schema_to_avro_schema(
    beam_schema: schema_pb2.Schema) -> _AvroSchemaType:
  return beam_type_to_avro_type(
      schema_pb2.FieldType(row_type=schema_pb2.RowType(schema=beam_schema)))


def beam_type_to_avro_type(beam_type: schema_pb2.FieldType) -> _AvroSchemaType:
  type_info = beam_type.WhichOneof("type_info")
  if type_info == "atomic_type":
    avro_primitive = BEAM_PRIMITIVES_TO_AVRO_PRIMITIVES[beam_type.atomic_type]
    if beam_type.nullable:
      return ['null', avro_primitive]
    else:
      return {'type': avro_primitive}
  elif type_info == "array_type":
    return {
        'type': 'array',
        'items': beam_type_to_avro_type(beam_type.array_type.element_type)
    }
  elif type_info == "iterable_type":
    return {
        'type': 'array',
        'items': beam_type_to_avro_type(beam_type.iterable_type.element_type)
    }
  elif type_info == "map_type":
    if beam_type.map_type.key_type.atomic_type != schema_pb2.STRING:
      raise TypeError(
          f'Only strings allowd as map keys when converting to AVRO, '
          f'found {beam_type}')
    return {
        'type': 'map',
        'values': beam_type_to_avro_type(beam_type.map_type.element_type)
    }
  elif type_info == "row_type":
    return {
        'type': 'record',
        'name': beam_type.row_type.schema.id,
        'fields': [{
            'name': field.name, 'type': beam_type_to_avro_type(field.type)
        } for field in beam_type.row_type.schema.fields],
    }
  else:
    raise ValueError(f"Unconvertale type: {beam_type}")


def beam_row_to_avro_dict(
    avro_schema: _AvroSchemaType, beam_schema: schema_pb2.Schema):
  if isinstance(avro_schema, str):
    return beam_row_to_avro_dict({'type': avro_schema}, beam_schema)
  if avro_schema['type'] == 'record':
    return beam_value_to_avro_value(
        schema_pb2.FieldType(row_type=schema_pb2.RowType(schema=beam_schema)))
  else:
    convert = beam_value_to_avro_value(beam_schema)
    return lambda row: convert(row[0])


def beam_atomic_value_to_avro_atomic_value(avro_type: str, value):
  """convert a beam atomic value to an avro atomic value

  since numeric values are converted to unsigned in
  avro_atomic_value_to_beam_atomic_value we need to convert
  back to a signed number.
  """
  if avro_type == "int":
    return ctypes.c_int32(value).value
  elif avro_type == "long":
    return ctypes.c_int64(value).value
  else:
    return value


def beam_value_to_avro_value(
    beam_type: schema_pb2.FieldType) -> Callable[[Any], Any]:
  type_info = beam_type.WhichOneof("type_info")
  if type_info == "atomic_type":
    avro_type = BEAM_PRIMITIVES_TO_AVRO_PRIMITIVES[beam_type.atomic_type]
    return lambda value: beam_atomic_value_to_avro_atomic_value(
        avro_type, value)
  elif type_info == "array_type":
    element_converter = beam_value_to_avro_value(
        beam_type.array_type.element_type)
    return lambda value: [element_converter(e) for e in value]
  elif type_info == "iterable_type":
    element_converter = beam_value_to_avro_value(
        beam_type.iterable_type.element_type)
    return lambda value: [element_converter(e) for e in value]
  elif type_info == "map_type":
    if beam_type.map_type.key_type.atomic_type != schema_pb2.STRING:
      raise TypeError(
          f'Only strings allowed as map keys when converting from AVRO, '
          f'found {beam_type}')
    value_converter = beam_value_to_avro_value(beam_type.map_type.value_type)
    return lambda value: {k: value_converter(v) for (k, v) in value.items()}
  elif type_info == "row_type":
    converters = {
        field.name: beam_value_to_avro_value(field.type)
        for field in beam_type.row_type.schema.fields
    }
    return lambda value: {
        name: convert(getattr(value, name))
        for (name, convert) in converters.items()
    }
  elif type_info == "logical_type":
    return lambda value: value
  else:
    raise ValueError(f"Unrecognized type_info: {type_info!r}")
