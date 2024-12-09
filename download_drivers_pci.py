#!/usr/bin/env python3

import requests
import sqlite3
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

def update_db(driver_data, conn):
    insert_data = [(d['url'], d['digest'], d['updateID']) for d in driver_data.values()]
    print(f'adding {len(insert_data)} urls to db')
    c = conn.cursor()
    c.executemany('update drivers set download_url=?, download_digest=? where guid=?', insert_data)
    conn.commit()
    
def download_url_to_file(url, dest_dir, sess):
    local_filename = url.split('/')[-1]
    local_path = os.path.join(dest_dir, local_filename)
    if os.path.exists(local_path):
        print(f'file {local_filename} already exists, skipping')
        return False
    response = sess.get(url, stream=True)
    if response.status_code != 200:
        print(f'got status {response.status_code} for {url}')
        response.close()
        return False
    print(f'starting {url}')
    with open(local_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)
    print('done')
    response.close()
    return True

def download_worker(in_queue):
    pid = os.getpid()
    sess = requests.Session()
    print(f'worker {pid} started')
    while not in_queue.empty():
        url = in_queue.get()
        try:
            download_url_to_file(url, 'pci_downloads', sess)
        except Exception as e:
            traceback.print_exc()
        finally:
            in_queue.task_done()
    print(f'{pid}: queue empty, finishing')

if __name__ == "__main__":
    # Initialize the queue and database connection
    url_queue = Queue()
    conn = sqlite3.connect('drivers_pci.sqlite')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Fill the queue with URLs to download
    c.execute('select distinct download_url from drivers where download_url is not null')
    for row in c:
        url_queue.put(row['download_url'])

    print(f'main proc {os.getpid()}')

    # Start download workers
    with ThreadPoolExecutor(max_workers=6) as executor:
        for _ in range(6):
            executor.submit(download_worker, url_queue)

    url_queue.join()
    conn.close()
