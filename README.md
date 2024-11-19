# Windows Update Driver Scraping Scripts

Run the scripts in the following order:

```
python wucatalogscrape.py - creates the drivers.sqlite database and scrapes basic driver info into it
python fetch_driver_download_urls.py - scrapes and stores the driver download URls
python download_drivers.py - downloads the driver CAB files into the downloads/ directory
python extract.py downloads/*.cab - extracts *.inf and *.pdb files into the extracted/ directory
python anaylse_drivers.py - analyses the extracted files
```

