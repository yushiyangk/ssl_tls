from __future__ import annotations

import bisect
import hashlib
import pickle
import struct
from pathlib import Path

import flask


LOG_ROW_SIZE = 40
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
				index = pickle.load(index_in)
		else:
			self.index_file.touch()
			index = ([], [], 0)
		self.log_index: list[tuple[int, int]] = index[0]
		self.chunk_index: list[tuple[int, int]] = index[1]
		self.curr_seq: int = index[2]

		for t, idx, ext in [("log", self.log_index, LOG_FILE_EXTENSION), ("chunk", self.chunk_index, CHUNK_FILE_EXTENSION)]:
			if len(idx) == 0:
				file = self.store_dir / f"{self.name}.0.{ext}"
				if file.exists():
					raise RuntimeError(f"bad index {self.name}: {t} 0 exists but not indexed")
				file.touch()
				idx.append((0, 0))
				self._index_updated = True

		self.curr_log: int = self.log_index[-1][0]
		self.curr_log_file: Path = self.store_dir / f"{self.name}.{self.curr_log}.{LOG_FILE_EXTENSION}"
		self.curr_chunk: int = self.chunk_index[-1][0]
		self.curr_chunk_file: Path = self.store_dir / f"{self.name}.{self.curr_chunk}.{CHUNK_FILE_EXTENSION}"
		self.curr_offset: int = self.curr_chunk_file.stat().st_size

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
		new_file.touch()
		self.log_index.append((self.curr_seq, self.curr_log))

	def add_chunk(self):
		self._index_updated = True
		self.curr_chunk += 1
		new_file = self.store_dir / f"{self.name}.{self.curr_chunk}.{CHUNK_FILE_EXTENSION}"
		if new_file.exists():
			raise RuntimeError(f"bad index {self.name}: chunk {self.curr_chunk} exists but not indexed")
		new_file.touch()
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


def process_log(source_name: str, timestamp: int, message: str) -> int:
	source_id = Source.compute_id(source_name)
	if directory_file.exists() and directory_file.stat().st_size > 0:
		with open(directory_file, 'rb') as directory_in:
			directory: dict[bytes, str] = pickle.load(directory_in)
	else:
		directory = {}

	if source_id not in directory:
		directory[source_id] = source_name
		with open(directory_file, 'wb') as directory_out:
			pickle.dump(directory, directory_out)

	with Source(source_name, store_dir) as source:
		source.log(timestamp, message)

	return 200



def read_chunk(source_name: str, chunk: int, offset: int, length: int) -> str | None:
	chunk_file: Path = store_dir / f"{source_name}/{source_name}.{chunk}.{CHUNK_FILE_EXTENSION}"
	if not chunk_file.exists() or chunk_file.stat().st_size < offset + length:
		return None
	with open(chunk_file, 'rb') as chunk_in:
		chunk_in.seek(offset)
		message = chunk_in.read(length).decode('utf-8')
		return message


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
		process_log(source_name, timestamp, message)

	return flask.make_response("", 200)


@app.route("/<source_name>", methods=['POST'])
def log_from(source_name: str) -> flask.Response:
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
		process_log(source_name, timestamp, message)

	return flask.make_response("", 200)


@app.route("/<source_name>", methods=['GET'])
def get_logs(source_name):
	args = flask.request.args
	start_seq = args.get("after_seq", type=int)
	end_seq = args.get("before_seq", type=int)

	source_id = Source.compute_id(source_name)
	if not directory_file.exists() or directory_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(directory_file, 'rb') as directory_in:
		directory: dict[bytes, str] = pickle.load(directory_in)

	index_file: Path = store_dir / f"{source_name}/{source_name}.{INDEX_FILE_EXTENSION}"
	if source_id not in directory or not index_file.exists() or index_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(index_file, 'rb') as index_in:
		index = pickle.load(index_in)
	log_index: list[tuple[int, int]] = index[0]

	if start_seq is None:
		log_i = 0
	else:
		log_i = bisect.bisect_left(log_index, start_seq, key=lambda t: t[0])
		if len(log_index) >= log_i or (log_index[log_i][0] >= start_seq):
			log_i -= 1

	output = []
	terminate = False
	for i, (s, log) in enumerate(log_index[log_i:]):
		log_file: Path = store_dir / f"{source_name}/{source_name}.{log}.{LOG_FILE_EXTENSION}"
		if not log_file.exists():
			continue
		log_size = log_file.stat().st_size
		if log_size == 0:
			continue

		with open(log_file, 'rb') as log_in:
			if start_seq is not None and i == 0:
				low = 0
				high = log_size // LOG_ROW_SIZE - 1
				mid = (low + high) // 2
				found = False
				while low <= high:
					mid = (low + high) // 2
					log_in.seek(mid * LOG_ROW_SIZE)
					row = log_in.read(LOG_ROW_SIZE)
					s, timestamp, chunk, offset, length = struct.unpack(LOG_ROW_FORMAT, row)
					if s < start_seq:
						low = mid + 1
					elif s > start_seq:
						high = mid - 1
					else:  # s == start_seq
						found = True
						break

				# log_in pointer has advanced to the next row, read from that point, except
				if not found and s > start_seq:  # reread the previous row which corresponds to s
					log_in.seek(mid * LOG_ROW_SIZE)
			else:
				pass  # read from 0

			row = log_in.read(LOG_ROW_SIZE)
			while len(row) != 0:
				if len(row) != LOG_ROW_SIZE:
					return flask.make_response("", 500)
				seq, timestamp, chunk, offset, length = struct.unpack(LOG_ROW_FORMAT, row)
				if end_seq is not None and seq >= end_seq:
					terminate = True
					break
				message = read_chunk(source_name, chunk, offset, length)
				if message is None:
					return flask.make_response("", 500)
				output.append({
					"source": source_name,
					"sequence_number": seq,
					"timestamp": timestamp,
					"message": message,
				})
				row = log_in.read(LOG_ROW_SIZE)

		if terminate:
			break

	return flask.make_response(output, 200)


@app.route("/<source_name>/<int:seq>", methods=['GET'])
def get_seq(source_name: str, seq: int) -> flask.Response:
	source_id = Source.compute_id(source_name)
	if not directory_file.exists() or directory_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(directory_file, 'rb') as directory_in:
		directory: dict[bytes, str] = pickle.load(directory_in)

	index_file: Path = store_dir / f"{source_name}/{source_name}.{INDEX_FILE_EXTENSION}"
	if source_id not in directory or not index_file.exists() or index_file.stat().st_size == 0:
		return flask.make_response("", 404)
	with open(index_file, 'rb') as index_in:
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
		low = 0
		high = log_size // LOG_ROW_SIZE - 1
		while low <= high:
			mid = (low + high) // 2
			log_in.seek(mid * LOG_ROW_SIZE)
			row = log_in.read(LOG_ROW_SIZE)
			s, timestamp, chunk, offset, length = struct.unpack(LOG_ROW_FORMAT, row)
			if s < seq:
				low = mid + 1
			elif s > seq:
				high = mid - 1
			else:  # s == seq
				message = read_chunk(source_name, chunk, offset, length)
				if message is None:
					return flask.make_response("", 500)
				return flask.make_response({
					"source": source_name,
					"sequence_number": seq,
					"timestamp": timestamp,
					"message": message,
				}, 200)
		return flask.make_response("", 404)


app.run()
