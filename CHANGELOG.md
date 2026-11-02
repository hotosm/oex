# Changelog

All notable changes to this project will be documented in this file.

This project follows [Conventional Commits](https://www.conventionalcommits.org/)
and is auto-managed by [commitizen](https://commitizen-tools.github.io/commitizen/).

## 0.2.0 (2026-05-07)

### Feat

- **osm**: adds osm as support and refactors entire setup with modern python deps

### Fix

- **config**: adds config dataset inside CONFIG class
- **license**: fixes license url not being parsed issue

## 0.1.1 (2025-03-30)

### Fix

- **lcs**: adds layer creation option and fixes encoding issue

## 0.1.0 (2025-03-30)

### Feat

- **readme**: add default Python example for Overture2HDX configuration

### Fix

- **encode**: fixes bug on encoding and adds more attributes to datasets

## 0.0.6 (2025-03-30)

### Fix

- **file**: fixes bug on file too large
- **yaml**: fixes bug with categories in tphub

## 0.0.5 (2025-03-30)

### Fix

- **schema**: adds default schema and fixes issues with license
- **license**: adds license info in yaml

## 0.0.4 (2025-03-30)

### Fix

- **category**: adds category to hdx exports and fixes bug on shapefile generation

## 0.0.3 (2024-11-08)

### Fix

- **doc-generation**: fix for readme doc link to package

## 0.0.2 (2024-11-08)

### Fix

- **config**: config fix

## v0.4.19 (2026-11-02)

### Fix

- **simplication**: add simplication feature and buffer for countries
- **lint**: fix lint issues

## v0.4.18 (2026-10-20)

### Fix

- **oexcli**: added oexcli with sharede configuration

## v0.4.17 (2026-10-20)

### Fix

- **enhance**: enhances retry download ,fixes export time , fixes memory limit

## v0.4.16 (2026-10-06)

### Feat

- **theme**: add theme section , add report with map and data enabled ,add also support for the fileformats

## v0.4.15 (2026-09-12)

### Fix

- **fix**: fallback engine
- **kml**: fix kml lat lon reverse bug

## v0.4.14 (2026-09-06)

### Fix

- **feat**: add geoparquet support and also external dataset support
- **osm**: add osm source

## v0.4.13 (2026-09-03)

### Fix

- **raw**: fix rawdata api bundle
- **update**: remove update note

## v0.4.12 (2026-08-29)

### Fix

- **summary**: fixes summary to be passed form config
- **ordering**: add ordering to datasets
- **boundary**: add boundary support in html report

## v0.4.11 (2026-08-28)

### Fix

- **onthefly**: postpass support

## v0.4.10 (2026-08-13)

### Fix

- **oex**: cpu limit

## v0.4.9 (2026-08-10)

### Fix

- **hdx**: fix hdx pipeline when data is empty

## v0.4.8 (2026-08-09)

### Fix

- **latest**: fix the cache stale based on date

## v0.4.7 (2026-08-06)

### Fix

- **tm**: fix tm export

## v0.4.6 (2026-08-06)

### Fix

- **osm**: optional iso3

## v0.4.5 (2026-08-06)

### Fix

- **chore**: add tm export example config
- **tm**: fixes tm path

## v0.4.4 (2026-07-27)

### Fix

- **fix**: map full screen and combo report on hdx

## v0.4.3 (2026-07-21)

### Fix

- **hdx**: fix hdx push off and preflight checks

## v0.4.2 (2026-07-13)

### Fix

- **combined**: dataset for hdx , adds the mapview fixes onclick and legend

## v0.4.1 (2026-06-23)

### Fix

- **fix**: test cases for platform agnostic
- **download**: remove export path from file if it is uploaded to hdx

## v0.4.0 (2026-06-02)

### Feat

- **clip**: added clip to planet boundary

## v0.3.0 (2026-05-17)

### Feat

- **h3**: add h3 index join instead of spatial to prevent memory issue and add performance ad
- **temporal**: add temporal info inthe report
- **language**: add local language support with babel
- **s3**: add s3 upload feature
- **transliterate**: add feature to transliterate the language
- **pcodes**: add pcode integration in datasets

### Fix

- **skip-null-pcode-rows-scan**: pcodes
- **fallback**: add fallabck to neighbour h3 grid rather than geos for large datasets
- **ci**: fixes test cases added h3 context
- **adaptive**: parallel resources to docker with osm cli
- **pcode**: add semaphore on pcode spatial joins
- **pcode**: fix the pcode join in big tables
- **path**: add preflight to fix writeable path
- **filesize**: fixes file size and add resume option
- **country**: parquet
- **loader**: planet
- **schema**: hot
- **s3**: uploader on schema
- **source**: fix source string in overture
- **overture**: fix on transliteratue
- **sources**: add report formatting with two sources
- **hdx**: fix hdx push bug
- **bug**: fixes precommit lock file bug

## v0.2.1 (2026-05-07)

### Fix

- **boundary**: fix boundary override on the configuration

## v0.2.0 (2026-05-07)

### Feat

- **osm**: adds osm as support and refactors entire setup with modern python deps
- **readme**: add default Python example for Overture2HDX configuration

### Fix

- **config**: adds config dataset inside CONFIG class
- **license**: fixes license url not being parsed issue
- **lcs**: adds layer creation option and fixes encoding issue
- **encode**: fixes bug on encoding and adds more attributes to datasets
- **file**: fixes bug on file too large
- **yaml**: fixes bug with categories in tphub
- **schema**: adds default schema and fixes issues with license
- **license**: adds license info in yaml
- **category**: adds category to hdx exports and fixes bug on shapefile generation
- **doc-generation**: fix for readme doc link to package
- **config**: config fix
