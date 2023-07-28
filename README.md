# SSL/TLS

Simple Service for Logging/Trivial Logging Service

Simple but fast storage backend suitable for very high-volume message streams.

(The name is a joke. This is not a cryptographic library.)

## Start server

Run `python src/ssl_tls/ssl_tls.py`

## Log messages

Send a POST request to `/` with either a single record or an array of records, where each record is an object that contains the following fields:
<pre><code>{
	"source": "<var>source_name</var>",
	"timestamp": <var>int</var>,
	"message": "<var>message</var>"
}
</code></pre>

Alternatively, send a POST request to <code>/<var>source_name</var></code> with either a single record or an array of records, where each record is an object that contains the following fields:
<pre><code>{
	"timestamp": <var>int</var>,
	"message": "<var>message</var>"
}
</code></pre>

## Get messages

Send a GET request to <code>/<var>source_name</var></code>.

To filter messages by index number, use the following optional parameters:
- <code>after_num=<var>N</var></code> – Get all messages with index number (strictly) greater than <code><var>N</var></code>
- <code>before_num=<var>M</var></code> – Get all messages with index number (strictly) less than <code><var>M</var></code>

The response will be an array of records, where each record is an object as follows:
<pre><code>{
	"message": "<var>message</var>",
	"number": <var>int</var>,
	"source": "<var>source_name</var>",
	"timestamp": <var>int</var>,
}
</code></pre>


Time-based filtering is an essential feature but is not implemented yet.

### Get single message

If the index number of the message is known, send a GET request to <code>/<var>source_name</var>/<var>num</var></code>.

The response will be a single record, as above.
