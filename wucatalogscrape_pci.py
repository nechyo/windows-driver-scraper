import logging
import requests
from requests import Request
from lxml import html
import re
import datetime
import traceback 
import sqlite3
from multiprocessing import Pool, Manager
import os

# 로그 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("driver_scraper_pci.log"),
        logging.StreamHandler()
    ]
)

WU_CAT_URL = 'http://catalog.update.microsoft.com/v7/site/Search.aspx'
IE_USER_AGENT = 'Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko'

def load_vendor_ids(filename):
    vids = {}
    with open(filename, 'r') as f:
        for line in f:
            if '#' in line.strip() or line.strip() == '':
                continue
            #print(line)
            vid, name = re.split('\s+', line.strip(), 1)
            vids[vid] = name
    return vids

conn = None
if not os.path.exists('drivers_pci.sqlite'):
    logging.info('creating schema')
    conn = sqlite3.connect('drivers_pci.sqlite')
    with open('schema_pci.sql') as f:
        script = f.read()
        conn.executescript(script)
else:
    conn = sqlite3.connect('drivers_pci.sqlite')

conn.row_factory = sqlite3.Row


vids = load_vendor_ids('pcivendorids.txt')
#vids = {'04f9':1, '0424':1}

vids_done = {}#get_completed_vids()



def parse_summary(tree):
    #1 - 25 of 294 (page 1 of 12)
    #1 - 6 of 6 (page 1 of 1)
    result_summary = tree.cssselect('[id$=searchDuration]')
    if not result_summary:
        return None
    summary_text = result_summary[0].text_content().strip()
    result = re.match('\d+ - \d+ of (\d+) \(page (\d+) of (\d+)\)', summary_text)
    if not result:
        return None
    total_results, current_page, total_pages = list(map(int, result.groups()))
    return (total_results, current_page, total_pages)
    
  
def parse_driver_table(tree):
    result_table = tree.cssselect('table[id$=updateMatches]')[0]

    COL_IGNORE = (0,7)
    COL_TITLE = 1
    COL_PRODUCTS = 2
    COL_CLASSIFICATION = 3
    COL_DATE = 4
    COL_VERSION = 5
    COL_SIZE = 6

    result_rows = result_table.findall('tr')[1:] # first row is headings
    for tr in result_rows:
        driver = {}
        for col, td in enumerate(tr.findall('td')):
            if col in COL_IGNORE:
                continue
            val = td.text_content().strip()
            if col == COL_TITLE:
                link = td.find('a')
                result = re.search('"([a-f0-9-]+)"', link.get('onclick'))
                if result:
                    driver['guid'] = result.group(1)
                driver['title'] = val
            if col == COL_SIZE:
                val = int(td.cssselect('[id$=originalSize]')[0].text_content())
                driver['size'] = val
            if col == COL_DATE:
                month, day, year = list(map(int, val.split('/')))
                val = datetime.date(year, month, day)
                driver['date'] = val
            if col == COL_VERSION:
                driver['version'] = val
            if col == COL_PRODUCTS:
                driver['products'] = val
            if col == COL_CLASSIFICATION:
                driver['classification'] = val
                
        yield driver


def prepare_driver_req(vid):
    params = {'q':'pci\\ven_%s' % vid}
    headers = {'user-agent': IE_USER_AGENT}
    req = Request('GET', WU_CAT_URL, params=params, headers=headers)
    return req.prepare()
    
def prepare_postback(response, button, tree = None):
    url = response.request.url
    if tree is None:
        tree = html.fromstring(response.text)
    try:
        viewstate = tree.cssselect('input[name=__VIEWSTATE]')[0].get('value')
        event_validation = tree.cssselect('input[name=__EVENTVALIDATION]')[0].get('value')
        postdata = {'__VIEWSTATE': viewstate, '__EVENTVALIDATION': event_validation, '__EVENTTARGET': button}
    except Exception as e:
        traceback.logging.info_exc()
        raise Exception("Couldn't find postback fields for %s" % url)
    headers = {'user-agent': IE_USER_AGENT}
    req = Request('POST', url, data=postdata, headers=headers)
    return req.prepare()

def parse_response(resp): 
    search_term = 'UNKNOWN'
    drivers = []
    next_page_req = None
    
    tree = html.fromstring(resp.text)
    summary = parse_summary(tree)

    search_term_result = tree.cssselect('[id$=searchString]')
    if search_term_result:
        search_term = search_term_result[0].text_content()
    
    if tree.cssselect('[id$=noResultText]'):
        return None
        
    if summary:
        total_results, current_page, total_pages = summary
        logging.info('%d results (page %d of %d) for %s' % (total_results, current_page, total_pages, search_term))

    for driver in parse_driver_table(tree):
        #logging.info(driver['products'])
        #if 'Windows 10' not in driver['products'] or 'Windows 11' not in driver['products']:
        #    continue
        drivers.append(driver)

    if summary and total_pages > current_page:
        next_page_req = prepare_postback(resp, 'ctl00$catalogBody$nextPageLinkText')
    return (drivers, current_page, total_pages, next_page_req)

def save_drivers(drivers, vid, conn):
    c = conn.cursor()
    for d in drivers:
        try:
            c.execute('insert into drivers (pci_vid, title, guid, date, version, classification, products, download_size) values (?,?,?,?,?,?,?,?)', 
                (vid, d['title'], d['guid'], d['date'], d['version'], d['classification'], d['products'], d['size']))
        except sqlite3.IntegrityError as e:
            logging.info("driver '%s', %s already in db" % (d['title'], d['guid']))
    
def log_visit(vid, page, total_pages, conn):
    c = conn.cursor()
    c.execute('insert into visited (vid, page, total_pages) values (?,?,?)', (vid, page, total_pages))
    

def get_completed_vids():
    c = conn.cursor()
    vids = {}
    c.execute('select vid, max(page) as p, max(total_pages) as tp from visited group by vid having page=0 or p=tp')
    for row in c:
        vids[row['vid']] = (row['p'], row['tp'])
    return vids

def request_worker(vid, result_queue):
    sess = requests.Session()
    try:
        req = prepare_driver_req(vid)
        while req:
            req = process_req(sess, req, vid, result_queue)
    except Exception as e:
        logging.error(f'Exception for VID {vid}: {e}')
        traceback.print_exc()

def process_req(sess, req, vid, result_queue):
    resp = sess.send(req)
    if resp.status_code == 200:
        results = parse_response(resp)
        if results:
            drivers, current_page, total_pages, next_req = results
            result_queue.put((vid, current_page, total_pages, drivers))
            logging.info(f'Fetched {len(drivers)} drivers for {vid}')
            return next_req 
    else:
        logging.error(f'Status {resp.status_code} for VID {vid}')
    return None

def main():
    # 데이터베이스 연결 설정
    conn = sqlite3.connect('drivers_pci.sqlite')
    conn.row_factory = sqlite3.Row
    vids = load_vendor_ids('pcivendorids.txt')
    vids_done = {}

    # 작업할 VID 설정
    vid_list = [vid for vid in vids if vid not in vids_done]

    logging.info(f'Total VIDs to process: {len(vid_list)}')

    # 매니저로 result_queue 생성
    with Manager() as manager:
        result_queue = manager.Queue()

        # Pool로 VID별 request_worker 실행
        with Pool(4) as pool:
            for vid in vid_list:
                pool.apply_async(request_worker, args=(vid, result_queue))

            pool.close()
            pool.join()

            # 결과 큐에서 데이터 저장
            while not result_queue.empty():
                vid, current_page, total_pages, drivers = result_queue.get()
                if drivers:
                    logging.info(f'Saving {len(drivers)} drivers for VID {vid}')
                    save_drivers(drivers, vid, conn)
                else:
                    logging.info(f'No drivers found for VID {vid}')
                log_visit(vid, current_page, total_pages, conn)
                conn.commit()

        # 데이터베이스 연결 종료
        conn.close()

if __name__ == '__main__':
    main()
