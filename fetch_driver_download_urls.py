import requests
import sqlite3
import re
import os
import traceback
from multiprocessing import Pool, Queue, JoinableQueue
from requests import Request, Session
from threading import Thread, local

# TODO: stop this script from stalling before it has fetched all the download URLs

# Create a thread-local storage to ensure each thread uses its own SQLite connection
thread_local = local()

def get_db_connection():
    if not hasattr(thread_local, "conn"):
        thread_local.conn = sqlite3.connect('drivers.sqlite')
        thread_local.conn.row_factory = sqlite3.Row
    return thread_local.conn

def chunks(l, n):
    """ Yield successive n-sized chunks from l. """
    for i in range(0, len(l), n):
        yield l[i:i+n]

def get_download_request(guids):
    updateIDs = ['{"updateID":"%s"}' % guid for guid in guids]
    updateIDs = '[%s]' % ','.join(updateIDs)
    req = Request('POST', WU_DOWNLOAD_URL, params={'updateIDs': updateIDs}, headers={'User-Agent': IE_USER_AGENT})
    return req.prepare()

def process_response(resp):
    results = re.findall(r'^downloadInformation\[(\d+)\].*(updateID|digest|url)\s*=\s*\'(.*)\'', resp.text, re.MULTILINE)
    driver_data = {}
    for result in results:
        id, key, value = result
        id = int(id)
        d = driver_data.get(id, {})
        d[key] = value
        driver_data[id] = d
        print(f"Processed response: {key} = {value}")
    return driver_data

def update_db(driver_data):
    insert_data = [(d['url'], d['digest'], d['updateID']) for d in driver_data.values()]
    print(f'Adding {len(insert_data)} URLs to the database.')
    conn = get_db_connection()
    with conn:
        c = conn.cursor()
        c.executemany('UPDATE drivers SET download_url=?, download_digest=? WHERE guid=?', insert_data)

def request_worker(in_queue, out_queue):
    pid = os.getpid()
    sess = Session()
    print(f'Worker {pid} started.')
    while True:
        request = in_queue.get()
        if request is None:
            break
        try:
            response = sess.send(request)
            if response.status_code == 200:
                data = process_response(response)
                out_queue.put(data)
            else:
                raise Exception(f'Status code {response.status_code} for request {request.url}')
        except Exception as e:
            print(f"Exception in worker {pid}: {e}")
            traceback.print_exc()
        finally:
            in_queue.task_done()

def get_guids_to_download():
    conn = get_db_connection()
    with conn:
        c = conn.cursor()
        guids = []
        c.execute('SELECT guid FROM drivers WHERE download_url IS NULL')
        for row in c.fetchall():
            guids.append(row['guid'])
    print(f"Retrieved {len(guids)} GUIDs to download.")
    return guids

def result_worker(result_queue):
    while True:
        try:
            driver_data = result_queue.get()
            if driver_data is None:
                break
            update_db(driver_data)
        except Exception as e:
            print(f"Exception in result worker: {e}")
            traceback.print_exc()
        finally:
            result_queue.task_done()

if __name__ == '__main__':
    WU_DOWNLOAD_URL = 'http://catalog.update.microsoft.com/v7/site/DownloadDialog.aspx'
    IE_USER_AGENT = 'Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko'

    request_queue = JoinableQueue()
    result_queue = JoinableQueue()

    guids = get_guids_to_download()
    grouped_guids = chunks(guids, 20)
    for g in grouped_guids:
        request_queue.put(get_download_request(g))

    # Start worker threads
    num_workers = 4
    workers = []
    for _ in range(num_workers):
        worker = Thread(target=request_worker, args=(request_queue, result_queue))
        worker.start()
        workers.append(worker)

    # Start result processing thread
    result_thread = Thread(target=result_worker, args=(result_queue,))
    result_thread.start()

    # Wait for all tasks to be done
    request_queue.join()
    result_queue.join()

    # Stop workers
    for _ in range(num_workers):
        request_queue.put(None)
    for worker in workers:
        worker.join()

    # Stop result thread
    result_queue.put(None)
    result_thread.join()
