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

import inspect
import os
import sys
from textwrap import dedent
from typing import Dict, List, IO, Tuple

from pyspark.accumulators import _accumulatorRegistry
from pyspark.errors import PySparkRuntimeError, PySparkValueError
from pyspark.java_gateway import local_connect_and_auth
from pyspark.serializers import (
    read_bool,
    read_int,
    write_int,
    write_with_length,
    SpecialLengths,
)
from pyspark.sql.functions import PartitioningColumn, SelectedColumn
from pyspark.sql.types import _parse_datatype_json_string, StructType
from pyspark.sql.udtf import AnalyzeArgument, AnalyzeResult
from pyspark.util import handle_worker_exception
from pyspark.worker_util import (
    check_python_version,
    read_command,
    pickleSer,
    send_accumulator_updates,
    setup_broadcasts,
    setup_memory_limits,
    setup_spark_files,
    utf8_deserializer,
)


def read_udtf(infile: IO) -> type:
    """Reads the Python UDTF and checks if its valid or not."""
    # Receive Python UDTF
    handler = read_command(pickleSer, infile)
    if not isinstance(handler, type):
        raise PySparkRuntimeError(
            f"Invalid UDTF handler type. Expected a class (type 'type'), but "
            f"got an instance of {type(handler).__name__}."
        )

    if not hasattr(handler, "analyze") or not isinstance(
        inspect.getattr_static(handler, "analyze"), staticmethod
    ):
        raise PySparkRuntimeError(
            "Failed to execute the user defined table function because it has not "
            "implemented the 'analyze' static method or specified a fixed "
            "return type during registration time. "
            "Please add the 'analyze' static method or specify the return type, "
            "and try the query again."
        )
    return handler


def read_arguments(infile: IO) -> Tuple[List[AnalyzeArgument], Dict[str, AnalyzeArgument]]:
    """Reads the arguments for `analyze` static method."""
    # Receive arguments
    num_args = read_int(infile)
    args: List[AnalyzeArgument] = []
    kwargs: Dict[str, AnalyzeArgument] = {}
    for _ in range(num_args):
        dt = _parse_datatype_json_string(utf8_deserializer.loads(infile))
        if read_bool(infile):  # is foldable
            value = pickleSer._read_with_length(infile)
            if dt.needConversion():
                value = dt.fromInternal(value)
        else:
            value = None
        is_table = read_bool(infile)  # is table argument
        argument = AnalyzeArgument(dataType=dt, value=value, isTable=is_table)

        is_named_arg = read_bool(infile)
        if is_named_arg:
            name = utf8_deserializer.loads(infile)
            kwargs[name] = argument
        else:
            args.append(argument)
    return args, kwargs


def main(infile: IO, outfile: IO) -> None:
    """
    Runs the Python UDTF's `analyze` static method.

    This process will be invoked from `UserDefinedPythonTableFunctionAnalyzeRunner.runInPython`
    in JVM and receive the Python UDTF and its arguments for the `analyze` static method,
    and call the `analyze` static method, and send back a AnalyzeResult as a result of the method.
    """
    try:
        check_python_version(infile)

        memory_limit_mb = int(os.environ.get("PYSPARK_PLANNER_MEMORY_MB", "-1"))
        setup_memory_limits(memory_limit_mb)

        setup_spark_files(infile)
        setup_broadcasts(infile)

        _accumulatorRegistry.clear()

        udtf_name = utf8_deserializer.loads(infile)
        handler = read_udtf(infile)
        args, kwargs = read_arguments(infile)

        error_prefix = f"Failed to evaluate the user-defined table function '{udtf_name}'"

        def format_error(msg: str) -> str:
            return dedent(msg).replace("\n", " ")

        # Check that the arguments provided to the UDTF call match the expected parameters defined
        # in the static 'analyze' method signature.
        try:
            inspect.signature(handler.analyze).bind(*args, **kwargs)  # type: ignore[attr-defined]
        except TypeError as e:
            # The UDTF call's arguments did not match the expected signature.
            raise PySparkValueError(
                format_error(
                    f"""
                    {error_prefix} because the function arguments did not match the expected
                    signature of the static 'analyze' method ({e}). Please update the query so that
                    this table function call provides arguments matching the expected signature, or
                    else update the table function so that its static 'analyze' method accepts the
                    provided arguments, and then try the query again."""
                )
            )

        # Invoke the UDTF's 'analyze' method.
        result = handler.analyze(*args, **kwargs)  # type: ignore[attr-defined]

        # Check invariants about the 'analyze' method after running it.
        if not isinstance(result, AnalyzeResult):
            raise PySparkValueError(
                format_error(
                    f"""
                    {error_prefix} because the static 'analyze' method expects a result of type
                    pyspark.sql.udtf.AnalyzeResult, but instead this method returned a value of
                    type: {type(result)}"""
                )
            )
        elif not isinstance(result.schema, StructType):
            raise PySparkValueError(
                format_error(
                    f"""
                    {error_prefix} because the static 'analyze' method expects a result of type
                    pyspark.sql.udtf.AnalyzeResult with a 'schema' field comprising a StructType,
                    but the 'schema' field had the wrong type: {type(result.schema)}"""
                )
            )
        has_table_arg = any(arg.isTable for arg in args) or any(
            arg.isTable for arg in kwargs.values()
        )
        if not has_table_arg and result.withSinglePartition:
            raise PySparkValueError(
                format_error(
                    f"""
                    {error_prefix} because the static 'analyze' method returned an
                    'AnalyzeResult' object with the 'withSinglePartition' field set to 'true', but
                    the function call did not provide any table argument. Please update the query so
                    that it provides a table argument, or else update the table function so that its
                    'analyze' method returns an 'AnalyzeResult' object with the
                    'withSinglePartition' field set to 'false', and then try the query again."""
                )
            )
        elif not has_table_arg and len(result.partitionBy) > 0:
            raise PySparkValueError(
                format_error(
                    f"""
                    {error_prefix} because the static 'analyze' method returned an
                    'AnalyzeResult' object with the 'partitionBy' list set to non-empty, but the
                    function call did not provide any table argument. Please update the query so
                    that it provides a table argument, or else update the table function so that its
                    'analyze' method returns an 'AnalyzeResult' object with the 'partitionBy' list
                    set to empty, and then try the query again."""
                )
            )
        elif isinstance(result.partitionBy, (list, tuple)) and (
            len(result.partitionBy) > 0
            and not all([isinstance(val, PartitioningColumn) for val in result.partitionBy])
        ):
            raise PySparkValueError(
                format_error(
                    f"""
                    {error_prefix} because the static 'analyze' method returned an
                    'AnalyzeResult' object with the 'partitionBy' field set to a value besides a
                    list or tuple of 'PartitioningColumn' objects. Please update the table function
                    and then try the query again."""
                )
            )
        elif isinstance(result.select, (list, tuple)) and (
            len(result.select) > 0
            and not all([isinstance(val, SelectedColumn) for val in result.select])
        ):
            raise PySparkValueError(
                format_error(
                    f"""
                    {error_prefix} because the static 'analyze' method returned an
                    'AnalyzeResult' object with the 'select' field set to a value besides a
                    list or tuple of 'SelectedColumn' objects. Please update the table function
                    and then try the query again."""
                )
            )

        # Return the analyzed schema.
        write_with_length(result.schema.json().encode("utf-8"), outfile)
        # Return the pickled 'AnalyzeResult' class instance.
        pickleSer._write_with_length(result, outfile)
        # Return whether the "with single partition" property is requested.
        write_int(1 if result.withSinglePartition else 0, outfile)
        # Return the list of partitioning columns, if any.
        write_int(len(result.partitionBy), outfile)
        for partitioning_col in result.partitionBy:
            write_with_length(partitioning_col.name.encode("utf-8"), outfile)
        # Return the requested input table ordering, if any.
        write_int(len(result.orderBy), outfile)
        for ordering_col in result.orderBy:
            write_with_length(ordering_col.name.encode("utf-8"), outfile)
            write_int(1 if ordering_col.ascending else 0, outfile)
            if ordering_col.overrideNullsFirst is None:
                write_int(0, outfile)
            elif ordering_col.overrideNullsFirst:
                write_int(1, outfile)
            else:
                write_int(2, outfile)
        # Return the requested selected input table columns, if specified.
        write_int(len(result.select), outfile)
        for col in result.select:
            write_with_length(col.name.encode("utf-8"), outfile)
            write_with_length(col.alias.encode("utf-8"), outfile)

    except BaseException as e:
        handle_worker_exception(e, outfile)
        sys.exit(-1)

    send_accumulator_updates(outfile)

    # check end of stream
    if read_int(infile) == SpecialLengths.END_OF_STREAM:
        write_int(SpecialLengths.END_OF_STREAM, outfile)
    else:
        # write a different value to tell JVM to not reuse this worker
        write_int(SpecialLengths.END_OF_DATA_SECTION, outfile)
        sys.exit(-1)


if __name__ == "__main__":
    # Read information about how to connect back to the JVM from the environment.
    java_port = int(os.environ["PYTHON_WORKER_FACTORY_PORT"])
    auth_secret = os.environ["PYTHON_WORKER_FACTORY_SECRET"]
    (sock_file, _) = local_connect_and_auth(java_port, auth_secret)
    main(sock_file, sock_file)
