#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License;
# you may not use this file except in compliance with the Elastic License.
#

import datetime
import hashlib
import unittest
import re
import struct
import ctypes

from elasticsearch import Elasticsearch
from data import TestData, BATTERS_TEMPLATE

UID = "elastic"
CONNECT_STRING = 'Driver={Elasticsearch Driver};UID=%s;PWD=%s;Secure=0;' % (UID, Elasticsearch.AUTH_PASSWORD)
CATALOG = "elasticsearch" # nightly built
#CATALOG = "distribution_run" # source built

class Testing(unittest.TestCase):

	_data = None
	_dsn = None
	_pyodbc = None

	def __init__(self, test_data, dsn=None):
		super().__init__()
		self._data = test_data
		self._dsn = dsn if dsn else CONNECT_STRING
		print("Using DSN: '%s'." % self._dsn)

		# only import pyODBC if running tests (vs. for instance only loading test data in ES)
		import pyodbc
		self._pyodbc = pyodbc

	def _reconstitute_csv(self, index_name):
		with self._pyodbc.connect(self._dsn) as cnxn:
			cnxn.autocommit = True
			csv = u""
			cols = self._data.csv_attributes(index_name)[1]
			fields = ",".join(cols)
			with cnxn.execute("select %s from %s" % (fields, index_name)) as curs:
				csv += u",".join(cols)
				csv += u"\n"
				for row in curs:
					vals = []
					for val in row:
						if val is None:
							val=""
						elif isinstance(val, datetime.datetime):
							val = val.isoformat() + "Z"
						vals.append(val)
					csv += u",".join(vals)
					csv += u"\n"

			return csv

	def _as_csv(self, index_name):
		print("Reconstituting CSV from index '%s'." % index_name)
		csv = self._reconstitute_csv(index_name)

		md5 = hashlib.md5()
		md5.update(bytes(csv, "utf-8"))
		csv_md5 = self._data.csv_attributes(index_name)[0]
		if md5.hexdigest() != csv_md5:
			print("Reconstituded CSV of index '%s':\n%s" % (index_name, csv))
			raise Exception("reconstituted CSV differs from original for index '%s'" % index_name)

	def _count_all(self, index_name):
		print("Counting records in index '%s.'" % index_name)
		cnt = 0
		with self._pyodbc.connect(self._dsn) as cnxn:
			cnxn.autocommit = True
			with cnxn.execute("select 1 from %s" % index_name) as curs:
				while curs.fetchone():
					cnt += 1
		csv_lines = self._data.csv_attributes(index_name)[2]
		if cnt != csv_lines:
			raise Exception("index '%s' only contains %s documents (vs original %s CSV lines)" %
					(index_name, cnt, csv_lines))

	def _clear_cursor(self, index_name):
		conn_str = self._dsn + ";MaxFetchSize=5"
		with self._pyodbc.connect(conn_str) as cnxn:
			cnxn.autocommit = True
			with cnxn.execute("select 1 from %s limit 10" % index_name) as curs:
				for i in range(3): # must be lower than MaxFetchSize, so no next page be requested
					curs.fetchone()
				# reuse the statment (a URL change occurs)
				with curs.execute("select 1 from %s" % index_name) as curs2:
					curs2.fetchall()
		# no exception raised -> passed

	def _select_columns(self, index_name, columns):
		print("Selecting columns '%s' from index '%s'." % (columns, index_name))
		with self._pyodbc.connect(self._dsn) as cnxn:
			cnxn.autocommit = True
			stmt = "select %s from %s" % (columns, index_name)
			with cnxn.execute(stmt) as curs:
				cnt = 0
				while curs.fetchone():
					cnt += 1 # no exception -> success
				print("Selected %s rows from %s." % (cnt, index_name))

	def _check_info(self, attr, expected):
		with self._pyodbc.connect(self._dsn) as cnxn:
			cnxn.autocommit = True
			value = cnxn.getinfo(attr)
			self.assertEqual(value, expected)

	# tables(table=None, catalog=None, schema=None, tableType=None)
	def _catalog_tables(self, no_table_type_as=""):
		with self._pyodbc.connect(self._dsn) as cnxn:
			cnxn.autocommit = True
			curs = cnxn.cursor()

			# enumerate catalogs
			res = curs.tables("", "%", "", no_table_type_as).fetchall()
			self.assertEqual(len(res), 1)
			for i in range(0,10):
				self.assertEqual(res[0][i], None if i else CATALOG)
			#self.assertEqual(res, [tuple([CATALOG] + [None for i in range(9)])]) # XXX?

			# enumerate table types
			res = curs.tables("", "", "", "%").fetchall()
			self.assertEqual(len(res), 2)
			for i in range(0,10):
				self.assertEqual(res[0][i], None if i != 3 else 'BASE TABLE')
				self.assertEqual(res[1][i], None if i != 3 else 'VIEW')

			# enumerate tables of selected type in catalog
			res = curs.tables(catalog="%", tableType="TABLE,VIEW").fetchall()
			res_tables = [row[2] for row in res]
			have_tables = [getattr(TestData, attr) for attr in dir(TestData)
					if attr.endswith("_INDEX") and type(getattr(TestData, attr)) == str]
			for table in have_tables:
				self.assertTrue(table in res_tables)

	# columns(table=None, catalog=None, schema=None, column=None)
	# use_surrogate: pyodbc seems to not reliably null-terminate the catalog and/or table name string,
	# despite indicating so.
	def _catalog_columns(self, use_catalog=False, use_surrogate=True):
		with self._pyodbc.connect(self._dsn) as cnxn:
			cnxn.autocommit = True
			curs = cnxn.cursor()
			if not use_surrogate:
				res = curs.columns(table=TestData.BATTERS_INDEX, catalog=CATALOG if use_catalog else None).fetchall()
			else:
				if use_catalog:
					stmt = "SYS COLUMNS CATALOG '%s' TABLE LIKE '%s' ESCAPE '\\' LIKE '%%' ESCAPE '\\'" % \
						(CATALOG, TestData.BATTERS_INDEX)
				else:
					stmt = "SYS COLUMNS TABLE LIKE '%s' ESCAPE '\\' LIKE '%%' ESCAPE '\\'" % TestData.BATTERS_INDEX
				res = curs.execute(stmt)
			cols_have = [row[3] for row in res]
			cols_have.sort()
			cols_expect = [k for k in BATTERS_TEMPLATE["mappings"]["properties"].keys()]
			cols_expect.sort()
			self.assertEqual(cols_have, cols_expect)


	# pyodbc doesn't support INTERVAL types; when installing an "output converter", it asks the ODBC driver for the
	# binary format and currently, this is the same as a wchar_t string for INTERVALs.
	# Also, just return None for data type 0 -- NULL
	def _install_output_converters(self, cnxn):
		wchar_sz = ctypes.sizeof(ctypes.c_wchar)
		if wchar_sz == ctypes.sizeof(ctypes.c_ushort):
			unit = "H"
		elif wchar_sz == ctypes.sizeof(ctypes.c_uint32):
			unit = "I"
		else:
			raise Exception("unsupported wchar_t size")

		# wchar_t to python string
		def _convert_interval(value):
			cnt = len(value)
			assert(cnt % wchar_sz == 0)
			cnt //= wchar_sz
			ret = ""
			fmt = "=" + str(cnt) + unit
			for c in struct.unpack(fmt, value):
				ret += chr(c)
			return ret

		for x in range(101, 114): # INTERVAL types IDs
			cnxn.add_output_converter(x, _convert_interval)

		def _convert_null(value):
			return None
		cnxn.add_output_converter(0, _convert_null) # NULL type ID

	# produce an instance of the 'data_type' out of the 'data_val' string
	def _type_to_instance(self, data_type, data_val):
		# Change the value read in the tests to type and format of the result expected to be
		# returned by driver.
		if data_type == "null":
			instance = None
		elif data_type.startswith("bool"):
			instance = data_val.lower() == "true"
		elif data_type in ["byte", "short", "integer"]:
			instance = int(data_val)
		elif data_type == "long":
			instance = int(data_val.strip("lL"))
		elif data_type == "double":
			instance = float(data_val)
		elif data_type == "float":
			instance = float(data_val.strip("fF"))
		elif data_type in ["datetime", "date", "time"]:
			fmt = "%H:%M:%S"
			fmt = "%Y-%m-%dT" + fmt
			# no explicit second with microseconds directive??
			if "." in data_val:
				fmt += ".%f"
			# always specify the timezone so that local-to-UTC conversion can take place
			fmt += "%z"
			val = data_val
			if data_type == "time":
				# parse Time as a Datetime, since some tests uses the ES/SQL-specific
				# Time-with-timezone which then needs converting to UTC (as the driver does).
				# and this conversion won't work for strptime()'ed Time values, as this uses
				# year 1900, not UTC convertible.
				val = "1970-02-02T" + val
			# strptime() won't recognize Z as Zulu/UTC
			val = val.replace("Z", "+00:00")
			instance = datetime.datetime.strptime(val, fmt)
			# if local time is provided, change it to UTC (as the driver does)
			try:
				timestamp = instance.timestamp()
				if data_type != "datetime":
					# The microsecond component only makes sense with Timestamp/Datetime with
					# ODBC (the TIME_STRUCT lacks a fractional second field).
					timestamp = int(timestamp)
				instance = instance.utcfromtimestamp(timestamp)
			except OSError:
				# The value can't be UTC converted, since the test uses Datetime years before
				# 1970 => convert it to timestamp w/o timezone.
				instance = datetime.datetime(instance.year, instance.month, instance.day,
						instance.hour, instance.minute, instance.second, instance.microsecond)

			if data_type == "date":
				instance = instance.date()
			elif data_type == "time":
				instance = instance.time()
		else:
			instance = data_val

		return instance

	def _proto_tests(self):
		tests = self._data.proto_tests()
		with self._pyodbc.connect(self._dsn) as cnxn:
			cnxn.autocommit = True
			self._install_output_converters(cnxn)
			try:
				for t in tests:
					(query, col_name, data_type, data_val, cli_val, disp_size) = t
					# print("T: %s, %s, %s, %s, %s, %s" % (query, col_name, data_type, data_val, cli_val, disp_size))
					with cnxn.execute(query) as curs:
						self.assertEqual(curs.rowcount, 1)
						res = curs.fetchone()[0]

						if data_val != cli_val: # INTERVAL tests
							assert(query.lower().startswith("select interval"))
							# extract the literal value (`INTERVAL -'1 1' -> `-1 1``)
							expect = re.match("[^-]*(-?\s*'[^']*').*", query).groups()[0]
							expect = expect.replace("'", "")
							# filter out tests with fractional seconds:
							# https://github.com/elastic/elasticsearch/issues/41635
							if re.search("\d*\.\d+", expect):
								continue
						else: # non-INTERVAL tests
							assert(data_type.lower() == data_type)
							# Change the value read in the tests to type and format of the result expected to be
							# returned by driver.
							expect = self._type_to_instance(data_type, data_val)

						self.assertEqual(res, expect)
			finally:
				cnxn.clear_output_converters()

	def perform(self):
		self._check_info(self._pyodbc.SQL_USER_NAME, UID)
		self._check_info(self._pyodbc.SQL_DATABASE_NAME, CATALOG)

		# simulate catalog querying as apps do in ES/GH#40775 do
		self._catalog_tables(no_table_type_as = "")
		self._catalog_tables(no_table_type_as = None)
		self._catalog_columns(use_catalog = False, use_surrogate = True)
		self._catalog_columns(use_catalog = True, use_surrogate = True)

		self._as_csv(TestData.LIBRARY_INDEX)
		self._as_csv(TestData.EMPLOYEES_INDEX)

		self._count_all(TestData.CALCS_INDEX)
		self._count_all(TestData.STAPLES_INDEX)
		self._count_all(TestData.BATTERS_INDEX)

		self._clear_cursor(TestData.LIBRARY_INDEX)

		self._select_columns(TestData.FLIGHTS_INDEX, "*")
		self._select_columns(TestData.ECOMMERCE_INDEX, "*")
		self._select_columns(TestData.LOGS_INDEX, "*")

		self._proto_tests()

		print("Tests successful.")

# vim: set noet fenc=utf-8 ff=dos sts=0 sw=4 ts=4 tw=118 :
