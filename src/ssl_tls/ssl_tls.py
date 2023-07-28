from __future__ import annotations

import bisect
import hashlib
import io
import pickle
import struct
from pathlib import Path
from typing import NamedTuple

import flask


STORE_VERSION = 1
HEADER_LENGTH = 2
HEADER_FORMAT = '!H'
LOG_ROW_LENGTH = 40
LOG_ROW_FORMAT = '!QQQQQ'

DEFAULT_STORE_REL_DIR = Path(".ssl_tls")
DIRECTORY_FILE = "root.stdir"
INDEX_FILE_EXTENSION = "stidx"
LOG_FILE_EXTENSION = "stlog"
CHUNK_FILE_EXTENSION = "stchunk"


store_dir = Path.home() / DEFAULT_STORE_REL_DIR

if not store_dir.exists():
	store_dir.mkdir(parents=True)

directory_file = store_dir / DIRECTORY_FILE

app = flask.Flask(__name__)


class ChunkQuery(NamedTuple):
	seq: int
	offset: int
	length: int


def write_header(fout: io.BufferedWriter):
	fout.write(struct.pack(HEADER_FORMAT, STORE_VERSION))

def read_header(fin: io.BufferedReader):
	header = fin.read(HEADER_LENGTH)
	assert struct.unpack(HEADER_FORMAT, header) == (STORE_VERSION,)


class Source:
	def __init__(self, name: str, store_dir: Path):
		self._open: bool = False
		self._index_updated: bool = False
		self.name: str = name
		self.store_dir: Path = store_dir / name
		if not self.store_dir.exists():
			self.store_dir.mkdir(parents=True)
		self.index_file: Path = self.store_dir / f"{name}.{INDEX_FILE_EXTENSION}"

	def __enter__(self) -> Source:
		if self.index_file.exists() and self.index_file.stat().st_size > 0:
			with open(self.index_file, 'rb') as index_in:
				read_header(index_in)
				index = pickle.load(index_in)
		else:
			with open(self.index_file, 'wb') as index_out:
				write_header(index_out)
			index = ([], [], 0)
		self.log_index: list[tuple[int, int]] = index[0]
		self.chunk_index: list[tuple[int, int]] = index[1]
		self.curr_seq: int = index[2]

		for t, idx, ext in [("log", self.log_index, LOG_FILE_EXTENSION), ("chunk", self.chunk_index, CHUNK_FILE_EXTENSION)]:
			if len(idx) == 0:
				file = self.store_dir / f"{self.name}.0.{ext}"
				if file.exists():
					raise RuntimeError(f"bad index {self.name}: {t} 0 exists but not indexed")
				with open(file, 'wb') as file_out:
					write_header(file_out)
				idx.append((0, 0))
				self._index_updated = True

		self.curr_log: int = self.log_index[-1][0]
		self.curr_log_file: Path = self.store_dir / f"{self.name}.{self.curr_log}.{LOG_FILE_EXTENSION}"
		self.curr_chunk: int = self.chunk_index[-1][0]
		self.curr_chunk_file: Path = self.store_dir / f"{self.name}.{self.curr_chunk}.{CHUNK_FILE_EXTENSION}"
		self.curr_offset: int = self.curr_chunk_file.stat().st_size - HEADER_LENGTH

		self.log_out = open(self.curr_log_file, 'ab')
		self.chunk_out = open(self.curr_chunk_file, 'ab')
		self._open = True

		return self

	def __exit__(self, exc_type, exc_value, traceback):
		self._open = False
		self.log_out.close()
		self.chunk_out.close()
		if self._index_updated:
			with open(self.index_file, 'wb') as index_out:
				write_header(index_out)
				pickle.dump((self.log_index, self.chunk_index, self.curr_seq), index_out)

	def encode(self, message: str) -> bytes:
		return message.encode('utf-8')

	def pack(self, seq: int, timestamp: int, chunk: int, offset: int, length: int) -> bytes:
		return struct.pack(LOG_ROW_FORMAT, seq, timestamp, chunk, offset, length)

	def add_log(self):
		self._index_updated = True
		self.curr_log += 1
		new_file = self.store_dir / f"{self.name}.{self.curr_log}.{LOG_FILE_EXTENSION}"
		if new_file.exists():
			raise RuntimeError(f"bad index {self.name}: log {self.curr_log} exists but not indexed")
		with open(new_file, 'wb') as new_file_out:
			write_header(new_file_out)
		self.log_index.append((self.curr_seq, self.curr_log))

	def add_chunk(self):
		self._index_updated = True
		self.curr_chunk += 1
		new_file = self.store_dir / f"{self.name}.{self.curr_chunk}.{CHUNK_FILE_EXTENSION}"
		if new_file.exists():
			raise RuntimeError(f"bad index {self.name}: chunk {self.curr_chunk} exists but not indexed")
		with open(new_file, 'wb') as new_file_out:
			write_header(new_file_out)
		self.log_index.append((self.curr_seq, self.curr_chunk))

	def log(self, timestamp: int, message: str):
		if not self._open:
			raise RuntimeError("cannot append to closed log")

		msg = self.encode(message)
		length = len(msg)
		self.log_out.write(self.pack(self.curr_seq, timestamp, self.curr_chunk, self.curr_offset, length))
		self.chunk_out.write(msg)
		self.curr_seq += 1
		self._index_updated = True
		self.curr_offset += length

	@staticmethod
	def compute_id(name: str) -> bytes:
		return hashlib.sha1(name.encode('utf-8')).digest()


def log_message(source_name: str, timestamp: int, message: str) -> int:
	source_id = Source.compute_id(source_name)
	if directory_file.exists() and directory_file.stat().st_size > 0:
		with open(directory_file, 'rb') as directory_in:
			read_header(directory_in)
			directory: dict[bytes, str] = pickle.load(directory_in)
	else:
		directory = {}

	if source_id not in directory:
		directory[source_id] = source_name
		with open(directory_file, 'wb') as directory_out:
			write_header(directory_out)
			pickle.dump(directory, directory_out)

	with Source(source_name, store_dir) as source:
		source.log(timestamp, message)

	return 200



def read_chunk(source_name: str, chunk: int, queries: list[ChunkQuery]) -> list[str]:
	chunk_file: Path = store_dir / f"{source_name}/{source_name}.{chunk}.{CHUNK_FILE_EXTENSION}"
	if not chunk_file.exists():
		raise RuntimeError(f"bad log {source_name}: chunk {chunk} does not exist")
	chunk_size = chunk_file.stat().st_size
	with open(chunk_file, 'rb') as chunk_in:
		read_header(chunk_in)
		output = []
		for query in queries:
			if chunk_size < HEADER_LENGTH + query.offset + query.length:
				raise RuntimeError(f"bad log {source_name}: chunk {chunk} ended before msg (seq {query.seq}, offset {query.offset}, length {query.length})")
			chunk_in.seek(HEADER_LENGTH + query.offset)
			message = chunk_in.read(query.length).decode('utf-8')
			output.append(message)
		return output


@app.route("/", methods=['POST'])
def log() -> flask.Response:
	content_type = flask.request.headers['Content-Type']
	if content_type != 'application/json':
		return flask.make_response("", 415)

	json = flask.request.json
	if isinstance(json, dict):
		json = [json]
	if not isinstance(json, list):
		return flask.make_response("", 400)

	for entry in json:
		if set(entry.keys()) != {'source', 'timestamp', 'message'}:
			return flask.make_response("", 400)
		source_name = entry['source']
		timestamp = entry['timestamp']
		message = entry['message']
		log_message(source_name, timestamp, message)

	return flask.make_response("", 200)


@app.route("/<source_name>", methods=['POST'])
def log_source(source_name: str) -> flask.Response:
	content_type = flask.request.headers['Content-Type']
	if content_type != 'application/json':
		return flask.make_response("", 415)

	json = flask.request.json
	if isinstance(json, dict):
		json = [json]
	if not isinstance(json, list):
		return flask.make_response("", 400)

	for entry in json:
		if set(entry.keys()) != {'timestamp', 'message'}:
			return flask.Response("", status=400)
		timestamp = entry['timestamp']
		message = entry['message']
		log_message(source_name, timestamp, message)

	return flask.make_response("", 200)


@app.route("/<source_name>", methods=['GET'])
def get_logs(source_name):
	args = flask.request.args
	after_seq = args.get("after_seq", type=int)
	before_seq = args.get("before_seq", type=int)

	source_id = Source.compute_id(source_name)
	if not directory_file.exists() or directory_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(directory_file, 'rb') as directory_in:
		read_header(directory_in)
		directory: dict[bytes, str] = pickle.load(directory_in)

	index_file: Path = store_dir / f"{source_name}/{source_name}.{INDEX_FILE_EXTENSION}"
	if source_id not in directory or not index_file.exists() or index_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(index_file, 'rb') as index_in:
		read_header(index_in)
		index = pickle.load(index_in)
	log_index: list[tuple[int, int]] = index[0]

	# Binary search for starting log
	if after_seq is None:
		start_log = 0
	else:
		start_log = bisect.bisect_left(log_index, after_seq, key=lambda t: t[0])
		if len(log_index) >= start_log or (log_index[start_log][0] >= after_seq):
			start_log -= 1

	# Read logs sequentially
	output = []
	terminate = False
	for log_i, (log_start_seq, log) in enumerate(log_index[start_log:]):
		log_file: Path = store_dir / f"{source_name}/{source_name}.{log}.{LOG_FILE_EXTENSION}"
		if not log_file.exists():
			continue
		log_size = log_file.stat().st_size
		if log_size == 0:
			continue

		with open(log_file, 'rb') as log_in:
			read_header(log_in)

			# For the first log read, binary search for starting row
			if after_seq is not None and log_i == 0:
				low = 0
				high = log_size // LOG_ROW_LENGTH - 1
				mid = (low + high) // 2
				found = False
				seq = None
				while low <= high:
					mid = (low + high) // 2
					log_in.seek(HEADER_LENGTH + mid * LOG_ROW_LENGTH)
					row = log_in.read(LOG_ROW_LENGTH)
					seq, timestamp, chunk, offset, length = struct.unpack(LOG_ROW_FORMAT, row)
					if seq < after_seq:
						low = mid + 1
					elif seq > after_seq:
						high = mid - 1
					else:  # seq == start_seq
						found = True
						break

				# log_in pointer has advanced to the next row, read from that point, except when seq > after_seq
				if not found and seq is not None and seq > after_seq:  # reread seq, which is on the previous row
					log_in.seek(HEADER_LENGTH + mid * LOG_ROW_LENGTH)
			else:
				pass  # read from 0

			query_chunk = None
			records = []
			queries = []

			# Read rows sequentially
			row = log_in.read(LOG_ROW_LENGTH)
			while len(row) != 0:
				if len(row) != LOG_ROW_LENGTH:
					raise RuntimeError(f"bad log {source_name}.{log}: incorrect row length")
				seq, timestamp, chunk, offset, length = struct.unpack(LOG_ROW_FORMAT, row)
				if before_seq is not None and seq >= before_seq:
					terminate = True
					break

				if query_chunk is None:
					query_chunk = chunk
				elif chunk != query_chunk:
					# Read previous chunk
					messages = read_chunk(source_name, query_chunk, queries)
					for i in range(len(messages)):
						records[i]["message"] = messages[i]
					output += records

					query_chunk = chunk
					records = []
					queries = []

				records.append({
					"source": source_name,
					"sequence_number": seq,
					"timestamp": timestamp,
				})
				queries.append(ChunkQuery(seq, offset, length))

				row = log_in.read(LOG_ROW_LENGTH)

			if query_chunk is not None:
				messages = read_chunk(source_name, query_chunk, queries)
				for i in range(len(messages)):
					records[i]["message"] = messages[i]
				output += records

		if terminate:
			break

	return flask.make_response(output, 200)


@app.route("/<source_name>/<int:seq>", methods=['GET'])
def get_seq(source_name: str, seq: int) -> flask.Response:
	source_id = Source.compute_id(source_name)
	if not directory_file.exists() or directory_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(directory_file, 'rb') as directory_in:
		read_header(directory_in)
		directory: dict[bytes, str] = pickle.load(directory_in)

	index_file: Path = store_dir / f"{source_name}/{source_name}.{INDEX_FILE_EXTENSION}"
	if source_id not in directory or not index_file.exists() or index_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(index_file, 'rb') as index_in:
		read_header(index_in)
		index = pickle.load(index_in)
	log_index: list[tuple[int, int]] = index[0]

	log_i = bisect.bisect_left(log_index, seq, key=lambda t: t[0])
	if len(log_index) >= log_i or (log_index[log_i][0] >= seq):
		log_i -= 1
	log = log_index[log_i][1]
	log_file: Path = store_dir / f"{source_name}/{source_name}.{log}.{LOG_FILE_EXTENSION}"
	if not log_file.exists():
		return flask.make_response("", 404)
	log_size = log_file.stat().st_size
	if log_size == 0:
		return flask.make_response("", 404)

	with open(log_file, 'rb') as log_in:
		read_header(log_in)
		low = 0
		high = log_size // LOG_ROW_LENGTH - 1
		while low <= high:
			mid = (low + high) // 2
			log_in.seek(HEADER_LENGTH + mid * LOG_ROW_LENGTH)
			row = log_in.read(LOG_ROW_LENGTH)
			if len(row) != LOG_ROW_LENGTH:
				raise RuntimeError(f"bad log {source_name}.{log}: incorrect row length")
			s, timestamp, chunk, offset, length = struct.unpack(LOG_ROW_FORMAT, row)
			if s < seq:
				low = mid + 1
			elif s > seq:
				high = mid - 1
			else:  # s == seq
				message = read_chunk(source_name, chunk, [ChunkQuery(seq, offset, length)])
				return flask.make_response({
					"source": source_name,
					"sequence_number": seq,
					"timestamp": timestamp,
					"message": message,
				}, 200)
		return flask.make_response("", 404)


app.run()
