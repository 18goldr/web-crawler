#!/usr/bin/env python
# -*- coding: utf-8 -*-

# November 2019, Robert Gold
#
# Main module for crawling text, quiz and video components using edx-dl downloader. 
# Original source code is modified from: https://github.com/TokyoTechX/web-crawler
#===========================================================================================================

import argparse
import getpass
import json
import logging
import os
import pickle
import re
import sys
import string
import codecs
import subprocess
import pandas as pd
import tarfile
import shutil
import ffmpeg
import requests

from webvtt import WebVTT
from datetime import datetime
from functools import partial
from multiprocessing.dummy import Pool as ThreadPool
from bs4 import BeautifulSoup as BeautifulSoup
from six.moves.http_cookiejar import CookieJar
from six.moves.urllib.error import HTTPError, URLError
from six.moves.urllib.parse import urlencode
from six.moves.urllib.request import (
        urlopen,
        build_opener,
        install_opener,
        HTTPCookieProcessor,
        Request,
        urlretrieve,
)

from lib.common import (
        Unit,
        Video,
        ExitCode,
        DEFAULT_FILE_FORMATS,
)

from lib.parsing import (
        edx_json2srt,
        get_page_extractor,
        is_youtube_url,
)

from lib.utils import (
        clean_filename,
        directory_name,
        execute_command,
        get_filename_from_prefix,
        get_page_contents,
        get_page_contents_as_json,
        mkdir_p,
        remove_duplicates,
)

OPENEDX_SITES = {
        'edx': {
                'url': 'https://courses.edx.org',
                'courseware-selector': ('nav', {'aria-label': 'Course Navigation'})
        },
        'hkust': {
                'url': 'https://learn.familylearning.hk',
                'courseware-selector': ('nav', {'aria-label': 'Course Navigation'})
        }
}

BASE_URL = OPENEDX_SITES['edx']['url']
EDX_HOMEPAGE = BASE_URL + '/login_ajax'
LOGIN_API = BASE_URL + '/login_ajax'
DASHBOARD = BASE_URL + '/dashboard'
COURSEWARE_SEL = OPENEDX_SITES['edx']['courseware-selector']


def change_openedx_site(site_name):
        """
        Changes the openedx website for the given one via the key
        """
        global BASE_URL
        global EDX_HOMEPAGE
        global LOGIN_API
        global DASHBOARD
        global COURSEWARE_SEL
        
        sites = sorted(OPENEDX_SITES.keys())
        
        if site_name not in sites:
                logging.error("OpenEdX platform should be one of: %s", ', '.join(sites))
                sys.exit(ExitCode.UNKNOWN_PLATFORM)
                
        BASE_URL = OPENEDX_SITES[site_name]['url']
        EDX_HOMEPAGE = BASE_URL + '/login_ajax'
        LOGIN_API = BASE_URL + '/login_ajax'
        DASHBOARD = BASE_URL + '/dashboard'
        COURSEWARE_SEL = OPENEDX_SITES[site_name]['courseware-selector']


    
#Parse the arguments passed to the program on the command line.
def parse_args():
        
        parser = argparse.ArgumentParser(prog='edx-crawler', description='Crawling text from the OpenEdX platform')
        
        # optional arguments
        parser.add_argument('-url',
                            '--course-urls',
                            dest='course_urls',
                            nargs='*',
                            action='store',
                            required=True,
                            help='target course urls'
                            '(e.g., https://courses.edx.org/courses/course-v1:TokyoTechX+GeoS101x+2T2016/course/)')

        parser.add_argument('-u',
                            '--username',
                            dest='username',
                            required=True,
                            action='store',
                            help='your edX username (email)')

        parser.add_argument('-p',
                            '--password',
                            dest='password',
                            action='store',
                            help='your edX password'
                            'beware: it might be visible to other users on your system')
        
        parser.add_argument('-d',
                            '--html-dir',
                            dest='html_dir',
                            action='store',
                            help='directory to store data',
                            default='HTMLs')
        
        parser.add_argument('-x',
                            '--platform',
                            dest='platform',
                            action='store', 
                            help='default is edx platform',
                            default='edx')
        
        parser.add_argument('--filter-section',
                            dest='filter_section',
                            action='store',
                            default=None,
                            help='filters sections to be downloaded')
        
        parser.add_argument('--list-file-formats',
                            dest='list_file_formats',
                            action='store_true',
                            default=False,
                            help='list the default file formats extracted')
        
        parser.add_argument('--file-formats',
                            dest='file_formats',
                            action='store',
                            default=None,
                            help='appends file formats to be extracted (comma '
                            'separated)')
        
        parser.add_argument('--overwrite-file-formats',
                            dest='overwrite_file_formats',
                            action='store_true',
                            default=False,
                            help='if active overwrites the file formats to be '
                            'extracted')

        parser.add_argument('--sequential',
                            dest='sequential',
                            action='store_true',
                            default=False,
                            help='extracts the resources from the pages sequentially')

        parser.add_argument('--quiet',
                            dest='quiet',
                            action='store_true',
                            default=False,
                            help='omit as many messages as possible, only printing errors')
        
        parser.add_argument('--debug',
                            dest='debug',
                            action='store_true',
                            default=False,
                            help='print lots of debug information')
        parser.add_argument('--list-courses',
                            dest='list_courses',
                            action='store_true',
                            default=False,
                            help='list available courses')
        
        args = parser.parse_args()

        # Initialize the logging system first so that other functions
        # can use it right away.
        if args.debug:
                logging.basicConfig(level=logging.DEBUG, format='%(name)s[%(funcName)s] %(message)s')
        elif args.quiet:
                logging.basicConfig(level=logging.ERROR, format='%(name)s: %(message)s')
        else:
                logging.basicConfig(level=logging.INFO, format='%(message)s')

        return args


def _display_courses(courses):
        """
        List the courses that the user has enrolled.
        """
        logging.info('You can access %d courses', len(courses))

        for i, course in enumerate(courses, 1):
                logging.info('%2d - %s [%s]', i, course.name, course.id)
                logging.info('     %s', course.url)


def get_courses_info(url, headers):
        """
        Extracts the courses information from the dashboard.
        """
        logging.info('Extracting course information from dashboard.')

        page = get_page_contents(url, headers)
        page_extractor = get_page_extractor(url)
        courses = page_extractor.extract_courses_from_html(page, BASE_URL)

        logging.debug('Data extracted: %s', courses)

        return courses


def _get_initial_token(url):
        """
        Create initial connection to get authentication token for future
        requests.

        Returns a string to be used in subsequent connections with the
        X-CSRFToken header or the empty string if we didn't find any token in
        the cookies.
        """
        logging.info('Getting initial CSRF token.')

        cookiejar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookiejar))
        install_opener(opener)
        opener.open(url)

        for cookie in cookiejar:
                if cookie.name == 'csrftoken':
                        logging.info('Found CSRF token.')
                        return cookie.value

        logging.warning('Did not find the CSRF token.')
        return ''


def get_available_sections(url, headers):
        """
        Extracts the sections and subsections from a given url
        """
        logging.debug("Extracting sections for :" + url)

        page = get_page_contents(url, headers)
        page_extractor = get_page_extractor(url)

        sections = page_extractor.extract_sections_from_html(page, BASE_URL)

        logging.debug("Extracted sections: " + str(sections))
        
        return sections

def edx_login(url, headers, username, password):
        """
        Log in user into the openedx website.
        """
        logging.info('Logging into Open edX site: %s', url)

        post_data = urlencode({'email': username,
                                                   'password': password,
                                                   'remember': False}).encode('utf-8')

        request = Request(url, post_data, headers)
        response = urlopen(request)
        resp = json.loads(response.read().decode('utf-8'))
        return resp


def edx_get_headers():
        """
        Build the Open edX headers to create future requests.
        """
        logging.info('Building initial headers for future requests.')

        headers = {
                'User-Agent': 'edX-downloader/0.01',
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
                'Referer': EDX_HOMEPAGE,
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRFToken': _get_initial_token(EDX_HOMEPAGE),
        }

        logging.debug('Headers built: %s', headers)
        return headers


def extract_units(url, headers, file_formats):
        """
        Parses a webpage and extracts its resources e.g. video_url, sub_url, etc.
        """
        #logging.info("Processing '%s'", url)

        page = get_page_contents(url, headers)
        page_extractor = get_page_extractor(url)
        units = page_extractor.extract_units_from_html(page, BASE_URL, file_formats)
        return units


def extract_all_units_in_sequence(urls, headers, file_formats):
        """
        Returns a dict of all the units in the selected_sections: {url, units}
        sequentially, this is clearer for debug purposes
        """
        logging.info('Extracting all units information in sequentially.')
        logging.debug('urls: ' + str(urls))

        units = [extract_units(url, headers, file_formats) for url in urls]
        all_units = dict(zip(urls, units))
        return all_units

def extract_all_units_in_parallel(urls, headers, file_formats):
        """
        Returns a dict of all the units in the selected_sections: {url, units}
        in parallel
        """
        logging.info('Extracting all units information in parallel.')
        logging.debug('urls: ' + str(urls))

        mapfunc = partial(extract_units, file_formats=file_formats, headers=headers)
        pool = ThreadPool(16)
        units = pool.map(mapfunc, urls)
        pool.close()
        pool.join()
        all_units = dict(zip(urls, units))
        return all_units


def _display_sections_menu(course, sections):
        """
        List the weeks for the given course.
        """
        num_sections = len(sections)

        logging.info('%s [%s] has %d sections so far', course.name, course.id, num_sections)
        for i, section in enumerate(sections, 1):
                logging.info('%2d - Download %s videos', i, section.name)


def _filter_sections(index, sections):
        """
        Get the sections for the given index.

        If the index is not valid (that is, None, a non-integer, a negative
        integer, or an integer above the number of the sections), we choose all
        sections.
        """
        num_sections = len(sections)

        logging.info('Filtering sections')

        if index is not None:
                try:
                        index = int(index)
                        if index > 0 and index <= num_sections:
                                logging.info('Sections filtered to: %d', index)
                                return [sections[index - 1]]
                        else:
                                pass  # log some info here
                except ValueError:
                        pass   # log some info here
        else:
                pass  # log some info here
        return sections


def _display_sections(sections):
        """
        Displays a tree of section(s) and subsections
        """
        logging.info('Downloading %d section(s)', len(sections))

        for section in sections:
                logging.info('Section %2d: %s', section.position, section.name)
                for subsection in section.subsections:
                        logging.info('  %s', subsection.name)

                        for unit in subsection.units:
                                logging.info('    %s', unit.name)


def parse_courses(args, available_courses):
        """
        Parses courses options and returns the selected_courses.
        """
        if args.list_courses:
                _display_courses(available_courses)
                exit(ExitCode.OK)

        
        if len(args.course_urls) == 0:
                logging.error('You must pass the URL of at least one course, check the correct url with --list-courses')
                exit(ExitCode.MISSING_COURSE_URL)

        selected_courses = [available_course
                                                for available_course in available_courses
                                                for url in args.course_urls
                                                if available_course.url == url]
        if len(selected_courses) == 0:
                logging.error('You have not passed a valid course url, check the correct url with --list-courses')
                exit(ExitCode.INVALID_COURSE_URL)
        return selected_courses


def parse_sections(args, selections):
        """
        Parses sections options and returns selections filtered by
        selected_sections
        """
        if not args.filter_section:
                return selections

        filtered_selections = {selected_course:
                                                   _filter_sections(args.filter_section, selected_sections)
                                                   for selected_course, selected_sections in selections.items()}
        return filtered_selections


def parse_file_formats(args):
        """
        parse options for file formats and builds the array to be used
        """
        file_formats = DEFAULT_FILE_FORMATS

        if args.list_file_formats:
                logging.info(file_formats)
                exit(ExitCode.OK)

        if args.overwrite_file_formats:
                file_formats = []

        if args.file_formats:
                new_file_formats = args.file_formats.split(",")
                file_formats.extend(new_file_formats)

        logging.debug("file_formats: %s", file_formats)
        return file_formats


def _display_selections(selections):
        """
        Displays the course, sections and subsections to be downloaded
        """

        for selected_course, selected_sections in selections.items():
                logging.info('Downloading %s [%s]',
                                         selected_course.name, selected_course.id)
                _display_sections(selected_sections)


def parse_units(all_units):
        """
        Parses units options and corner cases
        """
        flat_units = [unit for units in all_units.values() for unit in units]
        if len(flat_units) < 1:
                logging.warning('No downloadable video found.')
                exit(ExitCode.NO_DOWNLOADABLE_VIDEO)


def remove_repeated_urls(all_units):
        """
        Removes repeated urls from the units, it does not consider subtitles.
        This is done to avoid repeated downloads.
        """
        existing_urls = set()
        filtered_units = {}
        for url, units in all_units.items():
                reduced_units = []
                for unit in units:
                        videos = []
                        for video in unit.videos:
                                # we don't analyze the subtitles for repetition since
                                # their size is negligible for the goal of this function
                                video_youtube_url = None
                                if video.video_youtube_url not in existing_urls:
                                        video_youtube_url = video.video_youtube_url
                                        existing_urls.add(video_youtube_url)

                                mp4_urls, existing_urls = remove_duplicates(video.mp4_urls, existing_urls)

                                if video_youtube_url is not None or len(mp4_urls) > 0:
                                        videos.append(Video(video_youtube_url=video_youtube_url,
                                                                                available_subs_url=video.available_subs_url,
                                                                                sub_template_url=video.sub_template_url,
                                                                                mp4_urls=mp4_urls))

                        resources_urls, existing_urls = remove_duplicates(unit.resources_urls, existing_urls)

                        if len(videos) > 0 or len(resources_urls) > 0:
                                reduced_units.append(Unit(videos=videos,
                                                                                  resources_urls=resources_urls))

                filtered_units[url] = reduced_units
        return filtered_units


def num_urls_in_units_dict(units_dict):
        """
        Counts the number of urls in a all_units dict, it ignores subtitles from
        its counting.
        """
        num_urls = 0

        for units in units_dict.values():
                for unit in units:
                        for video in unit.videos:
                                num_urls += int(video.video_youtube_url is not None)
                                num_urls += int(video.available_subs_url is not None)
                                num_urls += int(video.sub_template_url is not None)
                                num_urls += len(video.mp4_urls)
                        num_urls += len(unit.resources_urls)

        return num_urls


def save_urls_to_file(urls, filename):
        """
        Save urls to file. Filename is specified by the user. The original
        purpose of this function is to export urls into a file for external
        downloader.
        """
        file_ = sys.stdout if filename == '-' else open(filename, 'w')
        file_.writelines(urls)
        file_.close()


def extract_problem_comp(soup):

        tmp = []
        problem_flag = soup.findAll("div", {"data-block-type": "problem"})  ## filter problem component
        for problem_comp in problem_flag:
                dict_soup = problem_comp.find(attrs={"data-content":True}).attrs    ## search no-html parser part
                txt2html = BeautifulSoup(dict_soup["data-content"],'html.parser')       
                dict_soup["data-content"] = BeautifulSoup(txt2html.prettify(formatter=None),'html.parser') ## restore html parser 
                tmp.append( dict_soup["data-content"])    ## save each problem component in list 
        type_div = []
        text = ''
        for each_problem_content in tmp:
                        
                for s in each_problem_content.findAll(['h1','h2','h3','h4','h5','h6','p','label','legend']):     
                        text+=s.getText()+" " 
                
                ############################ search for type of problem(quiz) ######################################
                #### from obseavation, multichoice & checkbox use the same clase. The difference lie into type of input option
                ####                   fillblank & droplist use the same clase but different subclass
                #### class has two attribute located at the 4th layer ('div'), with attribute ['class'][<class> <subclass>]  
                try:
                        type_div_tmp = each_problem_content.findAll('div')[4]['class'][0]        
                        if type_div_tmp == 'choicegroup':    
                                multi_or_check = each_problem_content.findAll('input')[0].attrs['type']
                                if multi_or_check == 'checkbox':
                                        type_div_tmp ='checkbox'
                                else:
                                        type_div_tmp = 'multichoice' 
                        elif type_div_tmp == 'inputtype':
                                if each_problem_content.findAll('div')[4]['class'][1] == 'option-input':
                                        type_div_tmp = 'droplist'
                                else:
                                        type_div_tmp = 'fillblank' 
                except KeyError:
                        type_div_tmp = 'N/A'
                type_div.append(type_div_tmp)   ## append all list of problem types into type_div
        return text,type_div 
           
def crawl_units(subsection_page):
        unit = []
        tmp=[]
        unit_name =[]
        idx = 0
        while tmp is not None:
          id_name = "seq_contents_"+str(idx)
          tmp = subsection_page.find("div", {"id": id_name})
          #print ("tmp: %s\n", tmp)
          unit.append(tmp)
          idx = idx + 1 
        unit.remove(None)
        return unit


def videolen(yt_link):
        duration = 0
        ## error handling when Youtube video is not currently available
        try:
                duration_raw = subprocess.check_output(["youtube-dl",yt_link, "--get-duration"])
                timeformat = duration_raw.decode("utf-8").split(':')
                if len(timeformat) == 1:
                        duration = int(timeformat[0])
                elif len(timeformat) == 2:
                        duration = int(timeformat[0])*60+int(timeformat[1])
                else:
                        duration = int(timeformat[0])*3600+int(timeformat[1])*60+ int(timeformat[2])
        except subprocess.CalledProcessError as e:
                print("video link bug: Youtube link is not available")
        return duration


def vtt2json(vttfile):
        t_start_milli = []
        t_end_milli = []
        text = []
        for caption in WebVTT().read(vttfile):
                h,m,s,ms= re.split(r'[\.:]+', caption.start)
                t_start_milli.append(int(h)*3600*1000+int(m)*60*1000+int(s)*1000+int(ms))
                h,m,s,ms= re.split(r'[\.:]+', caption.end)
                t_end_milli.append(int(h)*3600*1000+int(m)*60*1000+int(s)*1000+int(ms))
                text.append(caption.text)
        dict_obj = dict({"start":t_start_milli,"end":t_end_milli,"text":text})
        return dict_obj


def YT_transcript(yt_link,key):
        transcript_raw = ''
        ## error handling when Youtube video is not currently available
        try:
                checksub = subprocess.check_output(["youtube-dl",yt_link, "--list-sub"])
                if 'has no subtitles' not in checksub.decode('utf-8'):
                        lang_ls = list(filter(None, checksub.decode("utf-8").split('Language formats\n')[2].split('\n')))
                        for lang in lang_ls:
                                if key in lang:
                                        sub_dl = subprocess.check_output(["youtube-dl", yt_link, "--skip-download", "--write-sub", "--sub-lang", key])
                                        #vttfile = re.sub(r'\n','',sub_dl.decode('utf-8').split('Writing video subtitles to: ')[1])
                                        files = os.listdir()
                                        vttfile = [name for name in files if name.endswith('vtt')]
                                        transcript_raw = vtt2json(vttfile[0])
                                        os.remove(vttfile[0])
        except subprocess.CalledProcessError as e:
                print ("transcript link bug: Youtube link is not available")
        return transcript_raw


def extract_speech_period(start_ls,end_ls):
        period_ls = []
        for start_time,end_time in zip(start_ls,end_ls):
                tmp_period = (int(end_time) - int(start_time))/1000
                period_ls.append(tmp_period)
        return period_ls

def extract_speech_times(start_ls, end_ls):
    period_ls = []

    for start_time, end_time in zip(start_ls, end_ls):
        period_ls.append((int(start_time)/1000, int(end_time)/1000))

    return period_ls

def extract_duration_from_non_YT_video(source_mp4,headers):
        print(source_mp4)
        file_name = 'trial_video.mp4' 
        #print(source_mp4)
        rsp = urlopen(Request(source_mp4, None, headers))
        with open(file_name,'wb') as f:
                f.write(rsp.read())
        probe = ffmpeg.probe(file_name)
        duration = probe['streams'][1]['duration']
        os.remove(file_name)
        #print(probe)
        return(duration)

def extract_video_component(args,coursename,headers,soup,section,subsection,unit):      
        
        video_flag = soup.findAll("div", {"data-block-type": "video"})
        video_meta_list = []
        for video_comp in video_flag:
                video_meta = dict()
                video = video_comp.find('div',{"data-metadata":True})
                txtjson = video['data-metadata']
                edx_video_id = video['id']
                txt2dict = json.loads(txtjson)
                start_time = txt2dict['start']
                yt_id = re.sub(r"1.00:", '', txt2dict['streams'])
                if len(txt2dict['streams']) == 0:
                        duration = txt2dict['duration']
                        yt_link = 'n/a'
                        video_source = [i for i in txt2dict['sources']]
                        if duration == 0:
                                try:
                                        duration = extract_duration_from_non_YT_video(video_source[0],headers)
                                except (HTTPError,URLError) as exception:
                                        print('     bug: cannot download video from edx site')
                                        duration = 'n/a'
                        video_meta.update({'section': section , 'subsection': subsection,
                                           'unit': unit, 'youtube_url': yt_link,'video_source': video_source[0],
                                           'video_duration': duration, 'video_id': edx_video_id, 'start': start_time})
                else:
                        yt_link = 'https://youtu.be/'+ yt_id
                        duration = videolen(yt_link)
                        video_source = 'n/a'
                        if duration == 0:
                                duration = txt2dict['duration']
                        video_meta.update({'section': section , 'subsection': subsection,
                                           'unit': unit, 'youtube_url':yt_link,'video_source': video_source,
                                           'video_duration':duration, 'video_id': edx_video_id, 'start': start_time})


                
                for key, value in txt2dict['transcriptLanguages'].items():
                        transcript_name = 'transcript_'+ key
                        transcript_url = BASE_URL + '/' + re.sub(r"__lang__",key, txt2dict['transcriptTranslationUrl']) 
                        if yt_link == 'n/a':
                                print('download '+ value + ' transcript of '+ video_source[0])
                        else:
                                print('download '+ value + ' transcript of '+ yt_link)
                        try:
                                transcript_dump = get_page_contents(transcript_url, headers)
                                transcript_raw = json.loads(transcript_dump)
                                #print (transcript_raw)
                                speech_period = extract_speech_period(transcript_raw['start'],transcript_raw['end'])
                                speech_times = extract_speech_times(transcript_raw['start'],transcript_raw['end'])
                
                                video_meta.update({
                                    transcript_name: transcript_raw['text'],
                                    'speech_period': speech_period,
                                    'speech_times': speech_times
                                })
                                
                        except (HTTPError,URLError) as exception:

                                print('     bug: cannot download transcript from edx site')
                                if yt_link == 'n/a':
                                        video_meta.update({transcript_name:{"start":'',"end":'',"text":''},'speech_period':'n/a'})
                                        logging.warning('transcript (error: %s)', exception)
                                        errorlog = os.path.join(args.html_dir,coursename,'transcript_error_report.txt')
                                        f = open(errorlog, 'a')
                                        text = '---------------------------------\n'\
                                        + 'transcript error: ' + str(exception) +'\n' \
                                        + 'video file: '+ video_source[0] +'\n' \
                                        + 'language: ' + value + '\n' \
                                        + 'section:  ' + section + '\n'\
                                        + 'subsection: ' + subsection + '\n'\
                                        + 'unit_idx: ' + unit + '\n' \
                                        +'---------------------------------'
                                        f.write(text)
                                        f.close()
                                        continue

                                print('     attempt to download transcript on Youtube')
                                transcript_raw = YT_transcript(yt_link,key)
                                if len(transcript_raw) == 0:
                                        print('     no transcript available on YouTube')
                                        video_meta.update({transcript_name:{"start":'',"end":'',"text":''},'speech_period':'n/a'})
                                        logging.warning('transcript (error: %s)', exception)
                                        errorlog = os.path.join(args.html_dir,coursename,'transcript_error_report.txt')
                                        f = open(errorlog, 'a')
                                        text = '---------------------------------\n'\
                                        + 'transcript error: ' + str(exception) +'\n' \
                                        + 'video url: '+ yt_link +'\n' \
                                        + 'language: ' + value + '\n' \
                                        + 'section:  ' + section + '\n'\
                                        + 'subsection: ' + subsection + '\n'\
                                        + 'unit_idx: ' + unit + '\n' \
                                        +'---------------------------------'
                                        f.write(text)
                                        f.close()
                                else:
                                        print('     transcript was successfuly downloaded from YouTube')
                                        speech_period = extract_speech_period(transcript_raw['start'],transcript_raw['end'])
                                        video_meta.update({transcript_name:transcript_raw['text'],'speech_period':speech_period})

                video_meta_list.append(video_meta)
        return video_meta_list




def save_html_to_file(args, selections, all_urls, headers):
        sub_idx = 0
        prob_type_set = []
        counter_video = 1
        counter_unit = 1
        txt_id = 1
        prob_id = 1
        video_id =  1
        comp_id = 1
        tmp_course_strut = dict()
        txt_dict_ls = dict()
        prob_dict_ls = dict()
        comp_dict_ls = dict()
        video_dict_ls = dict()
        for selected_course, selected_sections in selections.items():
                coursename = directory_name(selected_course.name)
                sourcepath = os.path.join(args.html_dir, coursename,'source_html_file')
                mkdir_p(sourcepath)
                #filename_meta = os.path.join(sourcepath, 'html_metadata.csv')
                
                metasec_ls = [[],[],[],[]]
                for selected_section in selected_sections:
                        section_dirname = "%02d-%s" % (selected_section.position,
                                                                                   selected_section.name)
                        tmp_course_strut['section'] = (section_dirname)

                        for subsection in selected_section.subsections:
                           
                                if subsection.name == None:
                                        subsection.name = 'Untitled'

                        
                                tmp_course_strut['subsection'] = (subsection.name)
                                #logging.info('url: '+ str(all_urls[sub_idx]) )
                                print(all_urls[sub_idx])
                                page = get_page_contents(str(all_urls[sub_idx]), headers)
                                soup = BeautifulSoup(page, "html.parser")

                                #div contains all units (seq_contents_#)
                                main_content=soup.find("div", {"class": "container"})

                                units = crawl_units(main_content)               

                                sub_idx = sub_idx+1

                                for idx,unit in enumerate(units):
                                        
                                        filename_template = str(counter_unit).zfill(4) +".html"
                                        filename = os.path.join(args.html_dir, coursename,'source_html_file', filename_template)


                                        try:
                                                file_ = sys.stdout if filename == '-' else codecs.open( filename, 'w', 'utf-8')
                                        except IOError as exc:
                                                f = open('downloading_error_report.txt', 'a')
                                                text = 'External command error ignored: ' +str(exc) + '\n\n'
                                                f.write(text)
                                                f.close()
                                                file_ = sys.stdout if filename == '-' else codecs.open( filename_template, 'w', 'utf-8')
                                        
                                        file_.writelines(unit.prettify(formatter=None))
                                        file_.close()

                                        
                                        
                                        soup = unit.prettify(formatter=None)
                                        soup = BeautifulSoup(soup, "html.parser")


                                        cur_unit = soup.find("h2",{"class": "hd hd-2 unit-title"}).getText()
                                        if cur_unit == None:
                                                cur_unit = 'Untitled'
                                        tmp_course_strut['unit'] = (cur_unit)

                                        logging.info('section: ' + tmp_course_strut['section'])
                                        logging.info('     subsection: ' + tmp_course_strut['subsection'])
                                        logging.info('                unit: ' + tmp_course_strut['unit'])
                                        

                                        metasec_ls[0].append(tmp_course_strut['section'])
                                        metasec_ls[1].append(tmp_course_strut['subsection'])
                                        metasec_ls[2].append(tmp_course_strut['unit'])
                                        metasec_ls[3].append(filename_template)
                                        
                                
                                        # select only html componert (disregard video, problem)
                                        html_flag = soup.findAll("div", {"data-block-type": "html"})
                                        if len(html_flag) > 0:
                                        
                                                #create file only when html component exists
                                                text=""
                                                for soup_component in html_flag:                                        
                                                        for s in soup_component.findAll(['h1','h2','h3','h4','h5','h6','p','li']):
                                                                text+=s.getText()+" "                                           
                                

                                                tmp_dict = {'text_block_'+str(txt_id).zfill(4):{'section': tmp_course_strut['section'] , 'subsection': tmp_course_strut['subsection'], 'unit': tmp_course_strut['unit'], 'content':text}}
                                                txt_dict_ls.update(tmp_dict)
                                                txt_id +=1
                                                

                                        # select only problem componert (disregard video, text)
                                        prob_txt,prob_types = extract_problem_comp(soup)
                                        
                                        if len(prob_txt) > 0:
                                                for prob_type in prob_types:
                                                        prob_type_set.append(prob_type+' \n')
                                                
                                                tmp_dict = {'quiz_block_'+str(prob_id).zfill(4):{'section': tmp_course_strut['section']  , 'subsection': tmp_course_strut['subsection'], 'unit': tmp_course_strut['unit'], 'content':prob_txt}}
                                                prob_dict_ls.update(tmp_dict)
                                                #print(tmp_dict)
                                                prob_id +=1

                                        tmp_video_dict = extract_video_component(args,coursename,headers,soup,tmp_course_strut['section'],tmp_course_strut['subsection'],tmp_course_strut['unit'])
                                        if len(tmp_video_dict) > 0:
                                                video_unit_dict = dict()
                                                for vd in tmp_video_dict:
                                                        video_unit_dict.update({"video_block_"+str(counter_video).zfill(4):vd})
                                                        counter_video +=1

                                                video_dict_ls.update(video_unit_dict)
                                                video_id +=1

                                        print(video_dict_ls)

                                        counter_unit += 1

                                        set_comp_types = soup.findAll("div", {"data-block-type":True})
                                        for comp_type in set_comp_types:
                                                if comp_type['data-block-type'] in ['html','video','problem']:
                                                        comp_dict = {str(comp_id).zfill(4)+'_'+comp_type['data-block-type']:{'section': tmp_course_strut['section']  , 'subsection': tmp_course_strut['subsection'], 'unit': tmp_course_strut['unit'], 'type': comp_type['data-block-type']}}
                                                        comp_dict_ls.update(comp_dict)
                                                        comp_id+=1

        txt_dict2json = json.dumps(txt_dict_ls, sort_keys=True, indent=4, separators=(',', ': '))
        prob_dict2json = json.dumps(prob_dict_ls, sort_keys=True, indent=4, separators=(',', ': '))
        video_dict2json = json.dumps(video_dict_ls, sort_keys=True, indent=4, separators=(',', ': '))
        comp_dict2json = json.dumps(comp_dict_ls, sort_keys=True, indent=4, separators=(',', ': '))

        with open(os.path.join(args.html_dir, coursename,'all_textcomp.json'),'w',encoding='utf-8') as f:
                f.write(txt_dict2json)

        with open(os.path.join(args.html_dir, coursename,'all_probcomp.json'),'w',encoding='utf-8') as f:
                f.write(prob_dict2json)

        with open(os.path.join(args.html_dir, coursename,'all_videocomp.json'),'w',encoding='utf-8') as f:
                f.write(video_dict2json)
        
        with open(os.path.join(args.html_dir, coursename,'all_comp.json'),'w',encoding='utf-8') as f:
                f.write(comp_dict2json)

        metafile_dict = {'section':metasec_ls[0],'subsection':metasec_ls[1],'unit':metasec_ls[2],'htmlfile':metasec_ls[3]}
        df = pd.DataFrame.from_dict(metafile_dict)
        df.to_csv(os.path.join(args.html_dir, coursename,'source_html_file','metadata.csv'))
        


        save_urls_to_file(prob_type_set,  os.path.join(args.html_dir, coursename,  "all_prob_type.txt"))
        make_tarfile(os.path.join(args.html_dir, coursename,'sourcefile.tar.gz'),os.path.join(args.html_dir, coursename,'source_html_file'))


def save_unit_urls_to_file(args, selections):
        logging.info("Saving unit urls to file")
        
        url_to_unit = {}
        
        for course, sections in selections.items():
                for section in sections:
                        for subsection in section.subsections:
                                for unit in subsection.units:
                                        url_to_unit[unit.name] = unit.url
                                        
        s = pd.Series(url_to_unit)
        with open(os.path.join(args.html_dir, directory_name(course.name), "extra_urls.csv"), "w") as f:
                s.to_csv(f, header=['url'], index_label='name')
                  

def make_tarfile(zip_path,sourcedir):
        print ("source file is being compressed as tar.gz ")
        with tarfile.open(zip_path, 'w:gz') as tar:
                for f in os.listdir(sourcedir):
                        tar.add(sourcedir + "/" + f, arcname=os.path.basename(f))
                tar.close()
        shutil.rmtree(sourcedir)
        
def correct_urls(selections):
        logging.info("Correcting unit urls")

        with requests.Session() as s:
                r = s.get(LOGIN_API)
                token = r.cookies['csrftoken']
                post_data = urlencode({'email': args.username,
                                       'password': args.password,
                                       'remember': False,
                                       'csrfmiddlewaretoken': token}).encode('utf-8')

                s.post(LOGIN_API, data=post_data, headers=headers)
                
                for course, sections in selections.items():
                        for section in sections:
                                for subsection in section.subsections:
                                        for unit in subsection.units:
                                                r = s.get(unit.url)
                                                unit.url = r.url


def main():
        start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        args = parse_args()
        file_formats = parse_file_formats(args)

        change_openedx_site(args.platform)

        # Query password, if not alredy passed by command line.
        if not args.password:
                args.password = getpass.getpass(stream=sys.stderr)

        if not args.username or not args.password:
                logging.error("You must supply username and password to log-in")
                exit(ExitCode.MISSING_CREDENTIALS)

        # Prepare Headers
        headers = edx_get_headers()

        # Login
        resp = edx_login(LOGIN_API, headers, args.username, args.password)
        if not resp.get('success', False):
                logging.error(resp.get('value', "Wrong Email or Password."))
                exit(ExitCode.WRONG_EMAIL_OR_PASSWORD)

        # Parse and select the available courses
        courses = get_courses_info(DASHBOARD, headers)
        available_courses = [course for course in courses if course.state == 'Started']
        selected_courses = parse_courses(args, available_courses)

        # Parse the sections and build the selections dict filtered by sections
        replace_with = 'course' if args.platform == 'edx' else 'courseware'

        all_selections = {course: get_available_sections(course.url.replace('info', replace_with), headers) for course in selected_courses}

        selections = parse_sections(args, all_selections)
        _display_selections(selections)

        correct_urls(selections)

                                        
        # Extract the unit information (downloadable resources)
        # This parses the HTML of all the subsection.url and extracts
        # the URLs of the resources as Units.
        # all_urls = [subsection.url
        #                         for selected_sections in selections.values()
        #                         for selected_section in selected_sections
        #                         for subsection in selected_section.subsections]
        
        # extractor = extract_all_units_in_parallel
        # if args.sequential:
        #         extractor = extract_all_units_in_sequence

        # all_units = extractor(all_urls, headers, file_formats)

        # parse_units(selections)

        # # This removes all repeated important urls
        # # FIXME: This is not the best way to do it but it is the simplest, a
        # # better approach will be to create symbolic or hard links for the repeated
        # # units to avoid losing information
        # filtered_units = remove_repeated_urls(all_units)
        # num_all_urls = num_urls_in_units_dict(all_units)
        # num_filtered_urls = num_urls_in_units_dict(filtered_units)
        # logging.warning('Removed %d duplicated urls from %d in total',
        #                          (num_all_urls - num_filtered_urls), num_all_urls)

        # #saving html content as course unit
        # save_html_to_file(args, selections, all_urls, headers)
        save_unit_urls_to_file(args, selections)
                
        
if __name__ == '__main__':
        try:
                main()
        except KeyboardInterrupt:
                logging.warning("\n\nCTRL-C detected, shutting down....")
                        
                sys.exit(ExitCode.OK)
