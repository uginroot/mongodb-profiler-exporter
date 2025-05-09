#!/usr/bin/env python3

import os
import logging
from prometheus_client import start_http_server, Counter, Gauge
import pymongo
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import argparse

# Configure logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

default_labels = ["db", "ns", "query_hash"]
# Prometheus metrics
slow_queries_count_total = Counter('slow_queries_count_total', 'Total number of slow queries', default_labels)
slow_queries_info = Gauge("slow_queries_info", "Information about slow query",
                         default_labels + ["query_shape", "query_framework", "op", "plan_summary"])
slow_queries_duration_total = Counter('slow_queries_duration_total', 'Total execution time of slow queries in milliseconds', default_labels)
slow_queries_keys_examined_total = Counter('slow_queries_keys_examined_total', 'Total number of examined keys', default_labels)
slow_queries_docs_examined_total = Counter('slow_queries_docs_examined_total', 'Total number of examined documents', default_labels)
slow_queries_nreturned_total = Counter('slow_queries_nreturned_total', 'Total number of returned documents', default_labels)

fields_to_metrics_map = {
    "millis": slow_queries_duration_total,
    "keysExamined": slow_queries_keys_examined_total,
    "docsExamined": slow_queries_docs_examined_total,
    "nreturned": slow_queries_nreturned_total
}

def connect_to_mongo(uri):
    client = pymongo.MongoClient(uri)
    return client

def get_query_hash_values(db, ns, start_time, end_time):
    profile_collection = db.system.profile
    # Find unique queryHash values
    unique_query_hashes = profile_collection.distinct("queryHash", {"ns": ns ,"ts": {"$gte": start_time, "$lt": end_time}})
    return unique_query_hashes

def get_ns_values(db, start_time, end_time):
    profile_collection = db.system.profile
    # Find unique ns values
    unique_ns_values = profile_collection.distinct("ns", {"ts": {"$gte": start_time, "$lt": end_time}})
    return unique_ns_values

def get_slow_queries_count(db, ns, query_hash, start_time, end_time):
    profile_collection = db.system.profile
    # Find values within the specified time window
    query = {"queryHash": query_hash, "ns": ns, "ts": {"$gte": start_time, "$lt": end_time}}
    count = profile_collection.count_documents(query)
    return count

def get_slow_queries_value_sum(db, ns, query_hash, start_time, end_time, fields):
    profile_collection = db.system.profile
    # Find values within the specified time window
    match_stage = {"$match": {"queryHash": query_hash, "ns": ns, "ts": {"$gte": start_time, "$lt": end_time}}}
    fields_formatted = {f"{field}": {"$sum": f"${field}"} for field in fields}
    group_stage = {"$group": {"_id": None, **fields_formatted}}
    query = [match_stage, group_stage]
    result = list(profile_collection.aggregate(query))[0]
    return {field: result.get(field, 0) for field in fields}

def remove_keys_and_replace(query, keys_to_remove, replace_value="?"):
    # Recursively remove keys and replace values in the query.
    if isinstance(query, dict):
        for key in keys_to_remove:
            query.pop(key, None)
        for key, value in query.items():
            query[key] = remove_keys_and_replace(value, keys_to_remove, replace_value)
    elif isinstance(query, list):
        for i, item in enumerate(query):
            query[i] = remove_keys_and_replace(item, keys_to_remove, replace_value)
    else:
        return replace_value
    return query

def get_query_info_values(db, ns, query_hash, start_time, end_time, keys_to_remove):
    # Get query information values for Prometheus metric.
    profile_collection = db.system.profile

    query = {"queryHash": query_hash,"ns": ns, "ts": {"$gte": start_time, "$lt": end_time}, "command.getMore": {"$exists": False}, "command.explain": {"$exists": False}}
    result = list(profile_collection.find(query).limit(1))
    if result:
        query = result[0].get("command", "")
        query_framework = result[0].get("queryFramework", "")
        op = result[0].get("op", "")
        plan_summary = result[0].get("planSummary", "")
        query_shape = remove_keys_and_replace(query, keys_to_remove)
    else:
        query_shape, query_framework, op, plan_summary = '', '', '', ''
    return [query_shape, query_framework, op, plan_summary]

def parse_args():
    parser = argparse.ArgumentParser(description='MongoDB Prometheus Exporter',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # Use environment variables or default values for command-line arguments
    parser.add_argument('--mongodb-uri', type=str, default=os.getenv('MONGODB_URI', 'mongodb://127.0.0.1:27017/'),
                        help='MongoDB URI')
    parser.add_argument('--wait-interval', type=int, default=os.getenv('WAIT_INTERVAL', 10),
                        help='Wait interval between data parsing in seconds')
    parser.add_argument('--max-string-size', type=int, default=os.getenv('MAX_STRING_SIZE', 1000),
                        help='Maximum string size for Prometheus labels')
    parser.add_argument('--listen-ip', type=str, default=os.getenv('LISTEN_IP', '0.0.0.0'),
                        help='IP address to listen on')
    parser.add_argument('--listen-port', type=int, default=os.getenv('LISTEN_PORT', 9179),
                        help='Port to listen')
    parser.add_argument('--verbose', action='store_true', default=os.getenv('VERBOSE', False),
                    help='Enable Verbose Mode')

    return parser.parse_args()

def main():
    args = parse_args()
    verbose = args.verbose
    keys_to_remove = ["cursor", "lsid", "projection", "limit", "signature", "$readPreference", "$db", "$clusterTime"]

    # Log important information
    logging.info(f"Starting MongoDB Prometheus Exporter with the following parameters:")
    logging.info(f"Wait Interval: {args.wait_interval} seconds")
    logging.info(f"Maximum String Size: {args.max_string_size}")
    logging.info(f"Listen IP: {args.listen_ip}")
    logging.info(f"Listen Port: {args.listen_port}")
    logging.info(f"Metrics Endpoint: /metrics")

    # Start Prometheus HTTP server
    start_http_server(args.listen_port, addr=args.listen_ip)

    slow_queries_info_last_cleared = datetime.now(ZoneInfo("UTC"))
    slow_queries_info_last_clear_interval=300 # seconds

    end_time = datetime.now(ZoneInfo("UTC"))
    start_time = end_time - timedelta(seconds=args.wait_interval)
    while True:
        loop_start = time.time()
        try:
            # Connect to MongoDB
            mongo_client = connect_to_mongo(args.mongodb_uri)
            if verbose:
                mongo_client.admin.command('ping')
                print("MongoDB Connection Status: Connected")

            # Calculate the time window
            end_time = datetime.now(ZoneInfo("UTC"))
            start_time = end_time - (end_time - start_time)

            # Get the list of databases
            databases = mongo_client.list_database_names()

            # Remove some dbs
            excluded_dbs = ["local", "admin", "config", "test"]
            valid_dbs = [db for db in databases if db not in excluded_dbs]
            if verbose: print(f"Discovered Databases: {valid_dbs}")
            if verbose: print(f"Start Queries Discovery in system.profile")

            # Iterate through valid databases and update metrics
            for db_name in valid_dbs:
                db = mongo_client[db_name]
                ns_values = get_ns_values(db, start_time, end_time)
                if ns_values:
                    if verbose: print(f"Discovered NS in {db_name} Database: {ns_values}")
                    for ns in ns_values:
                        query_hash_values = get_query_hash_values(db, ns, start_time, end_time)
                        if query_hash_values:
                            if verbose: print(f"Discovered queryHash in {ns}: {query_hash_values}")
                            for query_hash in query_hash_values:
                                count = get_slow_queries_count(db, ns, query_hash, start_time, end_time)
                                slow_queries_count_total.labels(db=db_name, ns=ns, query_hash=query_hash).inc(count)

                                slow_queries_values = get_slow_queries_value_sum(db, ns, query_hash, start_time, end_time, fields_to_metrics_map.keys())

                                for field, metric in fields_to_metrics_map.items():
                                    metric.labels(db=db_name, ns=ns, query_hash=query_hash).inc(slow_queries_values.get(field, 0))

                                query_info = get_query_info_values(db, ns, query_hash, start_time, end_time, keys_to_remove)

                                # Clear slow_queries_info metric to handle queries change, such as plan_summary update
                                if datetime.now(ZoneInfo("UTC")) - slow_queries_info_last_cleared >= timedelta(seconds=slow_queries_info_last_clear_interval):
                                    slow_queries_info._metrics.clear()
                                    slow_queries_info_last_cleared = datetime.now(ZoneInfo("UTC"))
                                if query_info[0] != '':
                                    slow_queries_info.labels(db=db_name, ns=ns, query_hash=query_hash,
                                    query_shape=str(query_info[0])[:args.max_string_size],
                                    query_framework=query_info[1], op=query_info[2],
                                    plan_summary=str(query_info[3])[:args.max_string_size]).set(1)
                        else:
                             if verbose: print(f"Discovered no query_hash_values in {ns}")
                else:
                    if verbose: print(f"No namespaces (NS) found in the 'system.profile' collection of the '{db_name}' database.")


            # Close MongoDB connection
            mongo_client.close()

        except Exception as e:
            logging.error(f"Error: {e}")

        # Calculate the time taken to execute the command
        elapsed = time.time() - loop_start
        if verbose: print(f'Elapsed loop execution time: {elapsed:.2f} seconds\n')
        time.sleep(max(0, args.wait_interval - elapsed - 0.0005)) # try to keep loop at interval by extracting actual execution time

if __name__ == '__main__':
    main()
