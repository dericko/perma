import traceback
from collections import OrderedDict
from contextlib import contextmanager
import itertools
from pyquery import PyQuery

from http.client import CannotSendRequest
from urllib.error import URLError

import os
import os.path
import tempfile
import threading
import time
from datetime import datetime, timedelta
import urllib.parse
import re
import redis
import urllib.robotparser
from urllib3.util import is_connection_dropped
import errno
import tempdir
import socket
from socket import error as socket_error
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_failure
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, NoSuchElementException, NoSuchFrameException, TimeoutException
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.common.proxy import ProxyType, Proxy
from pyvirtualdisplay import Display
import warcprox
from warcprox.controller import WarcproxController
from warcprox.warcproxy import WarcProxyHandler
from warcprox.mitmproxy import ProxyingRecordingHTTPResponse
from warcprox.mitmproxy import http_client
from warcprox.mitmproxy import MitmProxyHandler, socks, ssl
import requests
from requests.structures import CaseInsensitiveDict
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from django.core.files.storage import default_storage
from django.core.mail import mail_admins
from django.db.models import F
from django.db.models.functions import Greatest
from django.conf import settings
from django.utils import timezone
from django.http import HttpRequest
from django.template.defaultfilters import pluralize

from perma.models import WeekStats, MinuteStats, Registrar, LinkUser, Link, Organization, Capture, \
    CaptureJob, UncaughtError, InternetArchiveItem, InternetArchiveFile
from perma.email import send_self_email
from perma.exceptions import PermaPaymentsCommunicationException
from perma.utils import (url_in_allowed_ip_range,
    preserve_perma_warc, write_warc_records_recorded_from_web,
    write_resource_record_from_asset,
    user_agent_for_domain, Sec1TLSAdapter, remove_whitespace,
    get_ia_session, ia_global_task_limit_approaching,
    ia_perma_task_limit_approaching, ia_bucket_task_limit_approaching, copy_file_data, date_range)
from perma import site_scripts

import logging
logger = logging.getLogger('celery.django')


###           ###
### Capturing ###
###           ###

### CONSTANTS ###

RESOURCE_LOAD_TIMEOUT = settings.RESOURCE_LOAD_TIMEOUT # seconds to wait for at least one resource to load before giving up on capture
ROBOTS_TXT_TIMEOUT = 30 # seconds to wait before giving up on robots.txt
ONLOAD_EVENT_TIMEOUT = 30 # seconds to wait before giving up on the onLoad event and proceeding as though it fired
ELEMENT_DISCOVERY_TIMEOUT = 2 # seconds before the browser gives up running a DOM request (should be instant, assuming page is loaded)
AFTER_LOAD_TIMEOUT = 25 # seconds to allow page to keep loading additional resources after onLoad event fires
SHUTDOWN_GRACE_PERIOD = settings.SHUTDOWN_GRACE_PERIOD # seconds to allow slow threads to finish before we complete the capture job
VALID_FAVICON_MIME_TYPES = {'image/png', 'image/gif', 'image/jpg', 'image/jpeg', 'image/x-icon', 'image/vnd.microsoft.icon', 'image/ico'}
BROWSER_SIZE = [1024, 800]


### ERROR REPORTING ###

@task_failure.connect()
def celery_task_failure_email(**kwargs):
    """
    Celery 4.0 onward has no method to send emails on failed tasks
    so this event handler is intended to replace it. It reports truly failed
    tasks, such as those terminated after CELERY_TASK_TIME_LIMIT.
    From https://github.com/celery/celery/issues/3389
    """

    subject = "[Django][{queue_name}@{host}] Error: Task {sender.name} ({task_id}): {exception}".format(
        queue_name='celery',
        host=socket.gethostname(),
        **kwargs
    )

    message = """Task {sender.name} with id {task_id} raised exception:
{exception!r}


Task was called with args: {args} kwargs: {kwargs}.

The contents of the full traceback was:

{einfo}
    """.format(
        **kwargs
    )
    mail_admins(subject, message)


### THREAD HELPERS ###

def add_thread(thread_list, target, **kwargs):
    if not isinstance(target, threading.Thread):
        target = threading.Thread(target=target, **kwargs)
    target.start()
    thread_list.append(target)
    return target

def safe_save_fields(instance, **kwargs):
    """
        Update and save the given fields for a model instance.
        Use update_fields so we won't step on changes to other fields made in another thread.
    """
    for key, val in kwargs.items():
        setattr(instance, key, val)
    instance.save(update_fields=list(kwargs.keys()))

def get_url(url, thread_list, proxy_address, requested_urls, proxied_responses, user_agent):
    """
        Get a url, via proxied python requests.get(), in a way that is interruptable from other threads.
        Blocks calling thread. (Recommended: only call in sub-threads.)
    """
    request_thread = add_thread(thread_list, ProxiedRequestThread(proxy_address, url, requested_urls, proxied_responses, user_agent))
    request_thread.join()
    return request_thread.response, request_thread.response_exception

class ProxiedRequestThread(threading.Thread):
    """
        Run python request.get() in a thread, loading data in chunks.
        Listen for self.stop to be set, allowing requests to be halted by other threads.
        While the thread is running, see `self.pending_data` for how much has been downloaded so far.
        Once the thread is done, see `self.response` and `self.response_exception` for the results.
    """
    def __init__(self, proxy_address, url, requested_urls, proxied_responses, user_agent, *args, **kwargs):
        self.url = url
        self.user_agent = user_agent
        self.proxy_address = proxy_address
        self.pending_data = 0
        self.stop = threading.Event()
        self.response = None
        self.response_exception = None
        self.requested_urls = requested_urls
        self.proxied_responses = proxied_responses
        super(ProxiedRequestThread, self).__init__(*args, **kwargs)

    def run(self):
        self.requested_urls.add(self.url)
        if self.proxied_responses["limit_reached"]:
            return
        try:
            with requests.Session() as s:

                # Lower our standards for the required TLS security level
                s.mount('https://', Sec1TLSAdapter())

                request = requests.Request(
                    'GET',
                    self.url,
                    headers={'User-Agent': self.user_agent, **settings.CAPTURE_HEADERS}
                )
                self.response = s.send(
                    request.prepare(),
                    proxies={'http': 'http://' + self.proxy_address, 'https': 'http://' + self.proxy_address},
                    verify=False,
                    stream=True,
                    timeout=1
                )
                self.response._content = bytes()
                for chunk in self.response.iter_content(chunk_size=8192):
                    self.pending_data += len(chunk)
                    self.response._content += chunk
                    if self.stop.is_set() or self.proxied_responses["limit_reached"]:
                        return
        except requests.RequestException as e:
            self.response_exception = e
        finally:
            if self.response:
                self.response.close()
            self.pending_data = 0

class HaltCaptureException(Exception):
    """
        An exception we can trigger to halt capture and release
        all involved resources.
    """
    pass


# WARCPROX HELPERS

# monkeypatch ProxyingRecorder to grab headers of proxied response
_orig_begin = ProxyingRecordingHTTPResponse.begin
def begin(self, extra_response_headers={}):
    _orig_begin(self, extra_response_headers={})
    self.recorder.headers = self.msg
ProxyingRecordingHTTPResponse.begin = begin

# get a copy of warcprox's proxy function, which we can use to
# monkey-patch the function freshly on each call of run_next_capture
_real_proxy_request = WarcProxyHandler._proxy_request

# get a copy of warcprox's connection function, which we can use to
# monkey-patch the function freshly on each call of run_next_capture
_orig_connect_to_remote_server = MitmProxyHandler._connect_to_remote_server

# BROWSER HELPERS

def start_virtual_display():
    display = Display(visible=0, size=BROWSER_SIZE)
    display.start()
    return display

def get_browser(user_agent, proxy_address, cert_path):
    """ Set up a Selenium browser with given user agent, proxy and SSL cert. """

    display = None
    print(f"Using browser: {settings.CAPTURE_BROWSER}")

    # Firefox
    if settings.CAPTURE_BROWSER == 'Firefox':
        display = start_virtual_display()

        desired_capabilities = dict(DesiredCapabilities.FIREFOX)
        proxy = Proxy({
            'proxyType': ProxyType.MANUAL,
            'httpProxy': proxy_address,
            'ftpProxy': proxy_address,
            'sslProxy': proxy_address,
        })
        proxy.add_to_capabilities(desired_capabilities)
        profile = webdriver.FirefoxProfile()
        profile.accept_untrusted_certs = True
        profile.assume_untrusted_cert_issuer = True
        browser = webdriver.Firefox(
            capabilities=desired_capabilities,
            firefox_profile=profile)

    # Chrome
    elif settings.CAPTURE_BROWSER == 'Chrome':
        # http://blog.likewise.org/2015/01/setting-up-chromedriver-and-the-selenium-webdriver-python-bindings-on-ubuntu-14-dot-04/
        # and from 2017-04-17: https://intoli.com/blog/running-selenium-with-headless-chrome/
        download_dir = os.path.abspath('./downloads')
        os.mkdir(download_dir)
        chrome_options = webdriver.ChromeOptions()
        chrome_options.add_argument(f'user-agent={user_agent}')
        chrome_options.add_argument(f'proxy-server={proxy_address}')
        chrome_options.add_argument('headless')
        chrome_options.add_argument('disable-gpu')
        chrome_options.add_argument('no-sandbox')
        chrome_options.add_argument('hide-scrollbars')
        chrome_options.add_experimental_option("prefs", {"profile.default_content_settings.popups": "0",
                                                         "download.default_directory": download_dir,
                                                         "download.prompt_for_download": "false"})
        if settings.DISABLE_DEV_SHM:
            chrome_options.add_argument('disable-dev-shm-usage')
        desired_capabilities = chrome_options.to_capabilities()
        desired_capabilities["acceptSslCerts"] = True
        browser = webdriver.Chrome(desired_capabilities=desired_capabilities)

    else:
        assert False, "Invalid value for CAPTURE_BROWSER."

    browser.implicitly_wait(ELEMENT_DISCOVERY_TIMEOUT)
    browser.set_page_load_timeout(ROBOTS_TXT_TIMEOUT)

    return browser, display

def browser_still_running(browser):
    return browser.service.process.poll() is None

def scroll_browser(browser):
    """scroll to bottom of page"""
    # TODO: This doesn't scroll horizontally or scroll frames
    try:
        scroll_delay = browser.execute_script("""
            // Scroll down the page in a series of jumps the size of the window height.
            // The actual scrolling is done in a setTimeout with a 50ms delay so the browser has
            // time to render at each position.
            var delay=50,
                height=document.body.scrollHeight,
                jump=window.innerHeight,
                scrollTo=function(scrollY){ window.scrollTo(0, scrollY) },
                i=1;
            for(;i*jump<height;i++){
                setTimeout(scrollTo, i*delay, i*jump);
            }

            // Scroll back to top before taking screenshot.
            setTimeout(scrollTo, i*delay, 0);

            // Return how long all this scrolling will take.
            return (i*delay)/1000;
        """)
        # In python, wait for javascript background scrolling to finish.
        time.sleep(min(scroll_delay,1))
    except (WebDriverException, TimeoutException, CannotSendRequest, URLError):
        # Don't panic if we can't scroll -- we've already captured something useful anyway.
        # WebDriverException: the page can't execute JS for some reason.
        # URLError: the headless browser has gone away for some reason.
        pass

def get_page_source(browser):
    """
        Get page source.
        Use JS rather than browser.page_source so we get the parsed, properly formatted DOM instead of raw user HTML.
    """
    try:
        return browser.execute_script("return document.documentElement.outerHTML")
    except (WebDriverException, TimeoutException, CannotSendRequest):
        return browser.page_source

def parse_page_source(source):
    """
        Return page source as a parsed PyQuery object for querying.

        PyQuery here works the same as `$(source)` in jQuery. So for example you can do `parsed_source(selector)`
        with the returned value to get a list of LXML elements matching the selector.
    """
    return PyQuery(source, parser='html')

def get_dom_tree(browser):
    with browser_running(browser):
        return parse_page_source(get_page_source(browser))

def get_all_dom_trees(browser):
    with browser_running(browser):
        return run_in_frames(browser, lambda browser: [[browser.current_url, get_dom_tree(browser)]])

def run_in_frames(browser, func, output_collector=None):
    # setup
    browser.implicitly_wait(0)

    if output_collector is None:
        output_collector = []
    run_in_frames_recursive(browser, func, output_collector)

    # reset
    browser.implicitly_wait(ELEMENT_DISCOVERY_TIMEOUT)
    browser.switch_to.default_content()

    return output_collector

def run_in_frames_recursive(browser, func, output_collector, frame_path=None):
    DEPTH_LIMIT = 3  # deepest frame level we'll visit
    FRAME_LIMIT = 20  # max total frames we'll visit
    if frame_path is None:
        frame_path = []

    with browser_running(browser):
        # slow to run, so only uncomment logging if needed for debugging:
        # import hashlib
        # print frame_path, browser.find_elements_by_tag_name('html')[0]._id, hashlib.sha256(browser.page_source.encode('utf8')).hexdigest(), browser.execute_script("return window.location.href")

        # attempt to get iframe url, skipping the iframe if attempt fails
        # (usually due to content security policy)
        try:
            current_url = browser.current_url
        except (WebDriverException, TimeoutException):
            return

        # skip about:blank, about:srcdoc, and any other non-http frames
        if not (current_url.startswith('http:') or current_url.startswith('https:')):
            return

        # run func in current frame
        output_collector += func(browser)

        # stop looking for subframes if we hit depth limit
        if len(frame_path) > DEPTH_LIMIT:
            return

        # run in subframes of current frame
        for i in range(FRAME_LIMIT):

            # stop looking for subframes if we hit total frames limit
            if len(output_collector) > FRAME_LIMIT:
                return

            # call self recursively in child frame i
            try:
                browser.switch_to.frame(i)
                run_in_frames_recursive(browser, func, output_collector, frame_path + [i])
            except NoSuchFrameException:
                # we've run out of subframes
                break
            except ValueError:
                # switching to frame failed for some reason (does this still apply?)
                print("run_in_frames_recursive caught exception switching to iframe:")
                traceback.print_exc()

            # return to current frame
            browser.switch_to.default_content()
            try:
                for frame in frame_path:
                    browser.switch_to.frame(frame)
            except NoSuchFrameException:
                # frame hierarchy changed; frame_path is invalid
                print("frame hierarchy changed while running run_in_frames_recursive")
                break


### UTILS ###

def repeat_while_exception(func, arglist=[], exception=Exception, timeout=10, sleep_time=.1, raise_after_timeout=True):
    """
       Keep running a function until it completes without raising an exception,
       or until "timeout" is reached.

       Useful when retrieving page elements via Selenium.
    """
    end_time = time.time() + timeout
    while True:
        try:
            return func(*arglist)
        except SoftTimeLimitExceeded:
            raise
        except exception:
            if time.time() > end_time:
                if raise_after_timeout:
                    raise
                return
            time.sleep(sleep_time)

def repeat_until_truthy(func, arglist=[], timeout=10, sleep_time=.1):
    """
        Keep running a function until it returns a truthy value, or until
        "timeout" is reached. No exception handling.

        Useful when retrieving page elements via javascript run by Selenium.
    """
    end_time = time.time() + timeout
    result = None
    while not result:
        if time.time() > end_time:
            break
        result = func(*arglist)
        time.sleep(sleep_time)
    return result

def sleep_unless_seconds_passed(seconds, start_time):
    delta = time.time() - start_time
    if delta < seconds:
        wait = seconds - delta
        print(f"Sleeping for {wait}s")
        time.sleep(wait)


# CAPTURE HELPERS

def inc_progress(capture_job, inc, description):
    capture_job.inc_progress(inc, description)
    print(f"{capture_job.link.guid} step {capture_job.step_count}: {capture_job.step_description}")

def capture_current_size(thread_list, recorded):
    """
        Amount captured so far is the sum of the bytes recorded by warcprox,
        and the bytes pending in our background threads.
    """
    return recorded + sum(getattr(thread, 'pending_data', 0) for thread in thread_list)

class CaptureCurrentSizeThread(threading.Thread):
    """
        Listen for self.stop to be set, allowing the thread to be halted by other threads.
    """
    def __init__(self, thread_list, proxied_responses, *args, **kwargs):
        self.stop = threading.Event()
        self.thread_list = thread_list
        self.proxied_responses = proxied_responses
        # include 'pending data' for a consistent API with other threads on the thread_list
        self.pending_data = 0
        super(CaptureCurrentSizeThread, self).__init__(*args, **kwargs)

    def run(self):
        while True:
            if self.stop.is_set():
                return
            if capture_current_size(self.thread_list, self.proxied_responses["size"]) > settings.MAX_ARCHIVE_FILE_SIZE:
                self.proxied_responses["limit_reached"] = True
                print("Size limit reached.")
                return
            time.sleep(.2)


def make_absolute_urls(base_url, urls):
    """collect resource urls, converted to absolute urls relative to current browser frame"""
    return [urllib.parse.urljoin(base_url, url) for url in urls if url]

def parse_headers(msg):
    """
    Given an http.client.HTTPMessage, returns a parsed dict
    """
    headers = CaseInsensitiveDict(msg.items())
    # Reset headers['x-robots-tag'], so that we can handle the
    # possibilility that multiple x-robots directives might be included
    # https://developers.google.com/webmasters/control-crawl-index/docs/robots_meta_tag
    # e.g.
    # HTTP/1.1 200 OK
    # Date: Tue, 25 May 2010 21:42:43 GMT
    # (...)
    # X-Robots-Tag: googlebot: nofollow
    # X-Robots-Tag: otherbot: noindex, nofollow
    # (...)
    # Join with a semi-colon, not a comma, so that multiple agents can
    # be recovered. As of 12/14/16, there doesn't appear to be any spec
    # describing how to do this properly (since commas don't work).
    # Since parsed response headers aren't archived, this convenience is
    # fine. However, it's worth keeping track of the situation.
    robots_directives = []
    # https://bugs.python.org/issue5053
    # https://bugs.python.org/issue13425
    directives = msg.get_all('x-robots-tag')
    if directives:
        for directive in directives:
            robots_directives.append(directive.replace("\n", "").replace("\r", ""))
    headers['x-robots-tag'] = ";".join(robots_directives)
    return headers


### CAPTURE COMPONENTS ###

# on load

# By domain, code to run after the target_url's page onload event.
post_load_function_lookup = {
    "^https?://www.forbes.com/forbes/welcome": site_scripts.forbes_post_load,
    "^https?://rwi.app/iurisprudentia": site_scripts.iurisprudentia_post_load,
}
def get_post_load_function(current_url):
    for regex, post_load_function in post_load_function_lookup.items():
        if re.search(regex, current_url.lower()):
            return post_load_function
    return None

# x-robots headers

def xrobots_blacklists_perma(robots_directives):
    darchive = False
    if robots_directives:
        for directive in robots_directives.split(";"):
            parsed = directive.lower().split(":")
            # respect tags that target all crawlers (no user-agent specified)
            if settings.PRIVATE_LINKS_IF_GENERIC_NOARCHIVE and len(parsed) == 1:
                if "noarchive" in parsed:
                    darchive = True
            # look for perma user-agent
            elif len(parsed) == 2:
                if parsed[0] == "perma" and "noarchive" in parsed[1]:
                    darchive = True
            # if the directive is poorly formed, do our best
            else:
                if "perma" in directive and "noarchive" in directive:
                    darchive = True
    return darchive

# page metadata

def get_metadata(page_metadata, dom_tree):
    """
        Retrieve html page metadata.
    """
    if page_metadata.get('title'):
        page_metadata['meta_tags'] = get_meta_tags(dom_tree)
    else:
        page_metadata.update({
            'meta_tags': get_meta_tags(dom_tree),
            'title': get_title(dom_tree)
        })

def get_meta_tags(dom_tree):
    """
        Retrieves meta tags as a dict (e.g. {"robots": "noarchive"}).

        The keys of the dict are the "name" attributes of the meta tags (if
        any) and the values are the corresponding "content" attributes.
        Later-encountered tags overwrite earlier-encountered tags, if a
        "name" attribute is duplicated in the html. Tags without name
        attributes are thrown away (their "content" attribute is mapped
        to the key "", the empty string).
    """
    return {tag.attrib['name'].lower(): tag.attrib.get('content', '')
            for tag in dom_tree('meta')
            if tag.attrib.get('name')}

def get_title(dom_tree):
    return dom_tree('head > title').text()

def meta_tag_analysis_failed(link):
    """What to do if analysis of a link's meta tags fails"""
    if settings.PRIVATE_LINKS_ON_FAILURE:
        safe_save_fields(link, is_private=True, private_reason='failure')
    print("Meta tag retrieval failure.")
    link.tags.add('meta-tag-retrieval-failure')

# robots.txt

def robots_txt_thread(link, target_url, content_url, thread_list, proxy_address, requested_urls, proxied_responses, user_agent):
    robots_txt_location = urllib.parse.urljoin(content_url, '/robots.txt')
    robots_txt_response, e = get_url(robots_txt_location, thread_list, proxy_address, requested_urls, proxied_responses, user_agent)
    if e or not robots_txt_response or not robots_txt_response.ok:
        print("Couldn't reach robots.txt")
        return
    print("Robots.txt fetched.")

    # We only want to respect robots.txt if Perma is specifically asked not to archive (we're not a crawler)
    content = str(robots_txt_response.content, 'utf-8')
    if 'Perma' in content:
        # We found Perma specifically mentioned
        rp = urllib.robotparser.RobotFileParser()
        rp.parse([line.strip() for line in content.split('\n')])
        if not rp.can_fetch('Perma', target_url):
            safe_save_fields(link, is_private=True, private_reason='policy')
            print("Robots.txt disallows Perma.")

# favicons

def favicon_thread(successful_favicon_urls, dom_tree, content_url, thread_list, proxy_address, requested_urls, proxied_responses, user_agent):
    favicon_urls = favicon_get_urls(dom_tree, content_url)
    for favicon_url in favicon_urls:
        favicon = favicon_fetch(favicon_url, thread_list, proxy_address, requested_urls, proxied_responses, user_agent)
        if favicon:
            successful_favicon_urls.append(favicon)
    if not successful_favicon_urls:
        print("Couldn't get any favicons")

def favicon_get_urls(dom_tree, content_url):
    """
        Retrieve favicon URLs from DOM.
    """
    urls = []  # order here matters so that we prefer meta tag favicon over /favicon.ico
    for el in dom_tree('link'):
        if el.attrib.get('rel', '').lower() in ("shortcut icon", "icon"):
            href = el.attrib.get('href')
            if href:
                urls.append(href)
    urls.append('/favicon.ico')
    urls = make_absolute_urls(content_url, urls)
    urls = list(OrderedDict((url, True) for url in urls).keys())  # remove duplicates without changing list order
    return urls

def favicon_fetch(url, thread_list, proxy_address, requested_urls, proxied_responses, user_agent):
    print(f"Fetching favicon from {url} ...")
    response, e = get_url(url, thread_list, proxy_address, requested_urls, proxied_responses, user_agent)
    if e or not response or not response.ok:
        print("Favicon failed:", e, response)
        return
    # apply mime type whitelist
    mime_type = response.headers.get('content-type', '').split(';')[0]
    if mime_type in VALID_FAVICON_MIME_TYPES:
        return (url, mime_type)

# media

def get_media_tags(dom_trees):
    urls = set()
    for base_url, dom_tree in dom_trees:
        print("Fetching images in srcsets")
        new_urls = get_srcset_image_urls(dom_tree)
        print("Fetching audio/video objects")
        new_urls += get_audio_video_urls(dom_tree)
        new_urls += get_object_urls(dom_tree)
        urls |= set(make_absolute_urls(base_url, new_urls))
    return urls

def get_srcset_image_urls(dom_tree):
    """
       Return all urls listed in img/src srcset attributes.
    """
    urls = []
    for el in dom_tree('img[srcset], source[srcset]'):
        for src in el.attrib.get('srcset', '').split(','):
            src = src.strip().split()[0]
            if src:
                urls.append(src)
    # Get src, too: Chrome (and presumably Firefox) doesn't do
    # this automatically.
    for el in dom_tree('img[src]'):
        urls.append(el.attrib.get('src', ''))
    return urls

def get_audio_video_urls(dom_tree):
    """
       Return urls listed in video/audio/embed/source tag src attributes.
    """
    urls = []
    for el in dom_tree('video, audio, embed, source'):
        src = el.attrib.get('src', '').strip()
        if src:
            urls.append(src)
    return urls

def get_object_urls(dom_tree):
    """
        Return urls in object tag data/archive attributes, as well as object -> param[name="movie"] tag value attributes.
        Urls will be relative to the object tag codebase attribute if it exists.
    """
    urls = []
    for el in dom_tree('object'):
        codebase_url = el.attrib.get('codebase')
        el_urls = [el.attrib.get('data', '')] + \
                  el.attrib.get('archive', '').split() + \
                  [param.attrib.get('value', '') for param in PyQuery(el)('param[name="movie"]')]
        for url in el_urls:
            url = url.strip()
            if url:
                if codebase_url:
                    url = urllib.parse.urljoin(codebase_url, url)
                urls.append(url)
    return urls

# screenshot

def get_screenshot(link, browser):
    page_size = get_page_size(browser)
    if page_pixels_in_allowed_range(page_size):

        if settings.CAPTURE_BROWSER == 'Chrome':
            # set window size to page size in Chrome, so we get a full-page screenshot:
            browser.set_window_size(max(page_size['width'], BROWSER_SIZE[0]), max(page_size['height'], BROWSER_SIZE[1]))

        return browser.get_screenshot_as_png()
    else:
        print(f"Not taking screenshot! Page size is {page_size}")
        safe_save_fields(link.screenshot_capture, status='failed')

def get_page_size(browser):
    try:
        return browser.execute_script("""
            var body = document.body;
            var html = document.documentElement;
            var height = Math.max(
                    body.scrollHeight,
                    body.offsetHeight,
                    html.clientHeight,
                    html.scrollHeight,
                    html.offsetHeight
            );
            var width = Math.max(
                    body.scrollWidth,
                    body.offsetWidth,
                    html.clientWidth,
                    html.scrollWidth,
                    html.offsetWidth
            );
            return {'height': height, 'width': width}
        """)
    except Exception:
        try:
            root_element = browser.find_element_by_tag_name('html')
        except (TimeoutException, NoSuchElementException, URLError, CannotSendRequest):
            try:
                root_element = browser.find_element_by_tag_name('frameset')
            except (TimeoutException, NoSuchElementException, URLError, CannotSendRequest):
                # NoSuchElementException: HTML structure is weird somehow.
                # URLError: the headless browser has gone away for some reason.
                root_element = None
        if root_element:
            try:
                return root_element.size
            except WebDriverException:
                # If there is no "body" element, a WebDriverException is thrown.
                # Skip the screenshot in that case: nothing to see
                pass

def page_pixels_in_allowed_range(page_size):
    return page_size and page_size['width'] * page_size['height'] < settings.MAX_IMAGE_SIZE

### CAPTURE COMPLETION

def teardown(link, thread_list, browser, display, warcprox_controller, warcprox_thread):
    print("Shutting down browser and proxies.")
    for thread in thread_list:
        # wait until threads are done
        if hasattr(thread, 'stop'):
            thread.stop.set()
        thread.join()
    if browser:
        if not browser_still_running(browser):
            link.tags.add('browser-crashed')
        browser.quit()
    if display:
        display.stop()  # shut down virtual display
    if warcprox_controller:
        warcprox_controller.stop.set() # send signals to shut down warc threads
        warcprox_controller.proxy.pool.shutdown(wait=False) # non-blocking
    if warcprox_thread:
        warcprox_thread.join()  # wait until warcprox thread is done

    # wait for stray MitmProxyHandler threads
    shutdown_time = time.time()
    while True:
        if time.time() - shutdown_time > SHUTDOWN_GRACE_PERIOD:
            break
        threads = threading.enumerate()
        print(f"{len(threads)} active threads.")
        if not any('MitmProxyHandler' in thread.name for thread in threads):
            break
        print("Waiting for MitmProxyHandler")
        time.sleep(1)

    if warcprox_controller:
        warcprox_controller.warc_writer_processor.writer_pool.close_writers()  # blocking


def process_metadata(metadata, link):
    ## Privacy Related ##
    meta_tag = metadata['meta_tags'].get('perma')
    if settings.PRIVATE_LINKS_IF_GENERIC_NOARCHIVE and not meta_tag:
        meta_tag = metadata['meta_tags'].get('robots')
    if meta_tag and 'noarchive' in meta_tag.lower():
        safe_save_fields(link, is_private=True, private_reason='policy')
        print("Meta found, darchiving")

    ## Page Description ##
    description_meta_tag = metadata['meta_tags'].get('description')
    if description_meta_tag:
        safe_save_fields(link, submitted_description=description_meta_tag[:300])

    ## Page Title
    safe_save_fields(link, submitted_title=metadata['title'][:2100])


def save_warc(warcprox_controller, capture_job, link, content_type, screenshot, successful_favicon_urls):
    # save a single warc, comprising all recorded recorded content and the screenshot
    recorded_warc_path = os.path.join(
        os.getcwd(),
        warcprox_controller.options.directory,
        f"{warcprox_controller.options.warc_filename}.warc.gz"
    )
    warc_size = []  # pass a mutable container to the context manager, so that it can populate it with the size of the finished warc
    with open(recorded_warc_path, 'rb') as recorded_warc_records, \
         preserve_perma_warc(link.guid, link.creation_timestamp, link.warc_storage_file(), warc_size) as perma_warc:
        # screenshot first, per Perma custom
        if screenshot:
            write_resource_record_from_asset(screenshot, link.screenshot_capture.url, link.screenshot_capture.content_type, perma_warc)
        # then recorded content
        write_warc_records_recorded_from_web(recorded_warc_records, perma_warc)

    # update the db to indicate we succeeded
    safe_save_fields(
        link,
        warc_size=warc_size[0]
    )
    safe_save_fields(
        link.primary_capture,
        status='success',
        content_type=content_type
    )
    if screenshot:
        safe_save_fields(
            link.screenshot_capture,
            status='success'
        )
    save_favicons(link, successful_favicon_urls)
    capture_job.mark_completed()


def save_favicons(link, successful_favicon_urls):
    if successful_favicon_urls:
        Capture(
            link=link,
            role='favicon',
            status='success',
            record_type='response',
            url=successful_favicon_urls[0][0],
            content_type=successful_favicon_urls[0][1].lower()
        ).save()
        print(f"Saved favicons {successful_favicon_urls}")

def clean_up_failed_captures():
    """
        Clean up any existing jobs that are marked in_progress but must have timed out by now, based on our hard timeout
        setting.
    """
    # use database time with a custom where clause to ensure consistent time across workers
    for capture_job in CaptureJob.objects.filter(status='in_progress').select_related('link').extra(
        where=[f"capture_start_time < now() - make_interval(secs => {settings.CELERY_TASK_TIME_LIMIT})"]
    ):
        capture_job.mark_failed("Timed out.")
        capture_job.link.captures.filter(status='pending').update(status='failed')
        capture_job.link.tags.add('hard-timeout-failure')

### CONTEXT MANAGERS

@contextmanager
def warn_on_exception(message="Exception in block:", exception_type=Exception):
    try:
        yield
    except SoftTimeLimitExceeded:
        raise
    except exception_type as e:
        print(message, e)

@contextmanager
def browser_running(browser, onfailure=None):
    if browser_still_running(browser):
        yield
    else:
        print("Browser crashed")
        if onfailure:
            onfailure()
        raise HaltCaptureException


### TASK ##

@shared_task
@tempdir.run_in_tempdir()
def run_next_capture():
    """
        Grab and run the next CaptureJob. This will keep calling itself until there are no jobs left.
    """
    clean_up_failed_captures()

    # get job to work on
    capture_job = CaptureJob.get_next_job(reserve=True)
    if not capture_job:
        return  # no jobs waiting
    try:
        # Start warcprox process. Warcprox is a MITM proxy server and needs to be running
        # before, during and after the headless browser.
        #
        # Start a headless browser to capture the supplied URL. Also take a screenshot if the URL is an HTML file.
        #
        # This whole function runs with the local dir set to a temp dir by run_in_tempdir().
        # So we can use local paths for temp files, and they'll just disappear when the function exits.

        # basic setup
        start_time = time.time()
        link = capture_job.link
        target_url = link.ascii_safe_url
        browser = warcprox_controller = warcprox_thread = display = screenshot = content_type = None
        have_content = have_html = False
        thread_list = []
        page_metadata = {}
        successful_favicon_urls = []
        requested_urls = set()  # all URLs we have requested -- used to avoid duplicate requests
        stop = False
        proxy = False

        capture_user_agent = user_agent_for_domain(link.url_details.netloc)
        print(f"Using user-agent: {capture_user_agent}")

        if settings.PROXY_CAPTURES and any(domain in link.url_details.netloc for domain in settings.DOMAINS_TO_PROXY):
            proxy = True
            print("Using proxy.")

        # A default title is added in models.py, if an api user has not specified a title.
        # Make sure not to override it during the capture process.
        if link.submitted_title != link.get_default_title():
            page_metadata = {
                'title': link.submitted_title
            }

        # Get started, unless the user has deleted the capture in the meantime
        inc_progress(capture_job, 0, "Starting capture")
        if link.user_deleted or link.primary_capture.status != "pending":
            capture_job.mark_completed('deleted')
            return
        capture_job.attempt += 1
        capture_job.save()

        # BEGIN WARCPROX SETUP

        # Create a request handler class that tracks requests and responses
        # via in-scope, shared mutable containers. (Patch inside capture function
        # so the containers are initialized empty for every new capture.)
        proxied_responses = {
            "any": False,
            "size": 0,
            "limit_reached": False
        }
        proxied_pairs = []
        tracker_lock = threading.Lock()

        # Patch Warcprox's inner proxy function to be interruptible,
        # to prevent thread leak and permit the partial capture of streamed content.
        # See https://github.com/harvard-lil/perma/issues/2019
        def stoppable_proxy_request(self, extra_response_headers={}):
            '''
            Sends the request to the remote server, then uses a ProxyingRecorder to
            read the response and send it to the proxy client, while recording the
            bytes in transit. Returns a tuple (request, response) where request is
            the raw request bytes, and response is a ProxyingRecorder.
            :param extra_response_headers: generated on warcprox._proxy_request.
            It may contain extra HTTP headers such as ``Warcprox-Meta`` which
            are written in the WARC record for this request.
            '''
            # Build request
            req_str = f'{self.command} {self.path} {self.request_version}\r\n'

            # Swallow headers that don't make sense to forward on, i.e. most
            # hop-by-hop headers. http://tools.ietf.org/html/rfc2616#section-13.5.
            # self.headers is an email.message.Message, which is case-insensitive
            # and doesn't throw KeyError in __delitem__
            for key in (
                    'Connection', 'Proxy-Connection', 'Keep-Alive',
                    'Proxy-Authenticate', 'Proxy-Authorization', 'Upgrade'):
                del self.headers[key]

            self.headers['Via'] = warcprox.mitmproxy.via_header_value(
                    self.headers.get('Via'),
                    self.request_version.replace('HTTP/', ''))

            # Add headers to the request
            # XXX in at least python3.3 str(self.headers) uses \n not \r\n :(
            req_str += '\r\n'.join(f'{k}: {v}' for (k,v) in self.headers.items())

            req = req_str.encode('latin1') + b'\r\n\r\n'

            # Append message body if present to the request
            if 'Content-Length' in self.headers:
                req += self.rfile.read(int(self.headers['Content-Length']))

            prox_rec_res = None
            start = time.time()
            try:
                self.logger.debug(f'sending to remote server req={req}')

                # Send it down the pipe!
                self._remote_server_conn.sock.sendall(req)

                prox_rec_res = ProxyingRecordingHTTPResponse(
                        self._remote_server_conn.sock, proxy_client=self.connection,
                        digest_algorithm=self.server.digest_algorithm,
                        url=self.url, method=self.command,
                        tmp_file_max_memory_size=self._tmp_file_max_memory_size)
                prox_rec_res.begin(extra_response_headers=extra_response_headers)

                buf = None
                while buf != b'':
                    try:
                        buf = prox_rec_res.read(65536)
                    except http_client.IncompleteRead as e:
                        self.logger.warn(f'{e} from {self.url}')
                        buf = e.partial

                    if buf:
                        proxied_responses["size"] += len(buf)

                    if (self._max_resource_size and
                            prox_rec_res.recorder.len > self._max_resource_size):
                        prox_rec_res.truncated = b'length'
                        self._remote_server_conn.sock.shutdown(socket.SHUT_RDWR)
                        self._remote_server_conn.sock.close()
                        self.logger.info(f'truncating response because max resource size {self._max_resource_size} bytes exceeded for URL {self.url}')
                        break
                    elif ('content-length' not in self.headers and
                           time.time() - start > 3 * 60 * 60):
                        prox_rec_res.truncated = b'time'
                        self._remote_server_conn.sock.shutdown(socket.SHUT_RDWR)
                        self._remote_server_conn.sock.close()
                        self.logger.info(f'reached hard timeout of 3 hours fetching url without content-length: {self.url}')
                        break

                    # begin Perma changes #
                    if stop:
                        prox_rec_res.truncated = b'length'
                        self._remote_server_conn.sock.shutdown(socket.SHUT_RDWR)
                        self._remote_server_conn.sock.close()
                        self.logger.info(f'truncating response because stop signal received while recording {self.url}')
                        break
                    # end Perma changes #

                self.log_request(prox_rec_res.status, prox_rec_res.recorder.len)
                # Let's close off the remote end. If remote connection is fine,
                # put it back in the pool to reuse it later.
                if not is_connection_dropped(self._remote_server_conn):
                    self._conn_pool._put_conn(self._remote_server_conn)

            except Exception as e:
                # A common error is to connect to the remote server successfully
                # but raise a `RemoteDisconnected` exception when trying to begin
                # downloading. Its caused by prox_rec_res.begin(...) which calls
                # http_client._read_status(). The connection fails there.
                # https://github.com/python/cpython/blob/3.7/Lib/http/client.py#L275
                # Another case is when the connection is fine but the response
                # status is problematic, raising `BadStatusLine`.
                # https://github.com/python/cpython/blob/3.7/Lib/http/client.py#L296
                # In both cases, the host is bad and we must add it to
                # `bad_hostnames_ports` cache.
                if isinstance(e, (http_client.RemoteDisconnected,
                                  http_client.BadStatusLine)):
                    host_port = self._hostname_port_cache_key()
                    with self.server.bad_hostnames_ports_lock:
                        self.server.bad_hostnames_ports[host_port] = 502
                    self.logger.info(f'bad_hostnames_ports cache size: {len(self.server.bad_hostnames_ports)}')

                # Close the connection only if its still open. If its already
                # closed, an `OSError` "([Errno 107] Transport endpoint is not
                # connected)" would be raised.
                if not is_connection_dropped(self._remote_server_conn):
                    self._remote_server_conn.sock.shutdown(socket.SHUT_RDWR)
                    self._remote_server_conn.sock.close()
                raise
            finally:
                if prox_rec_res:
                    prox_rec_res.close()

            return req, prox_rec_res

        warcprox.mitmproxy.MitmProxyHandler._inner_proxy_request = stoppable_proxy_request


        def _proxy_request(self):

            # make sure we don't capture anything in a banned IP range
            if not url_in_allowed_ip_range(self.url):
                return

            # skip request if downloaded size exceeds MAX_ARCHIVE_FILE_SIZE.
            if proxied_responses["limit_reached"]:
                return

            with tracker_lock:
                proxied_pair = [self.url, None]
                requested_urls.add(proxied_pair[0])
                proxied_pairs.append(proxied_pair)
            try:
                response = _real_proxy_request(self)
            except Exception as e:
                # If warcprox can't handle a request/response for some reason,
                # remove the proxied pair so that it doesn't keep trying and
                # the capture process can proceed
                proxied_pairs.remove(proxied_pair)
                print(f"WarcProx exception: {e.__class__.__name__} proxying {proxied_pair[0]}")
                return  # swallow exception
            with tracker_lock:
                if response:
                    proxied_responses["any"] = True
                    proxied_pair[1] = response
                else:
                    # in some cases (502? others?) warcprox is not returning a response
                    proxied_pairs.remove(proxied_pair)

        WarcProxyHandler._proxy_request = _proxy_request

        # patch warcprox's  to go through proxy whenever onion_tor_socks_proxy_host is set
        def _connect_to_remote_server(self):
            self._conn_pool = self.server.remote_connection_pool.connection_from_host(
                host=self.hostname, port=int(self.port), scheme='http',
                pool_kwargs={'maxsize': 12, 'timeout': self._socket_timeout})

            remote_ip = None

            self._remote_server_conn = self._conn_pool._get_conn()
            if is_connection_dropped(self._remote_server_conn):
                if self.onion_tor_socks_proxy_host:  # Perma removed `and self.hostname.endswith('.onion')`
                    self.logger.info(f"using tor socks proxy at {self.onion_tor_socks_proxy_host}:{self.onion_tor_socks_proxy_port or 1080} to connect to {self.hostname}")
                    self._remote_server_conn.sock = socks.socksocket()
                    self._remote_server_conn.sock.set_proxy(
                            socks.SOCKS5, addr=self.onion_tor_socks_proxy_host,
                            port=self.onion_tor_socks_proxy_port, rdns=True,
                            username="user", password=link.guid)  # Perma added username and password, to force new IPs
                    self._remote_server_conn.sock.settimeout(self._socket_timeout)
                    self._remote_server_conn.sock.connect((self.hostname, int(self.port)))
                else:
                    self._remote_server_conn.connect()
                    remote_ip = self._remote_server_conn.sock.getpeername()[0]

                # Wrap socket if SSL is required
                if self.is_connect:
                    try:
                        context = ssl.create_default_context()
                        context.check_hostname = False
                        context.verify_mode = ssl.CERT_NONE
                        self._remote_server_conn.sock = context.wrap_socket(
                                self._remote_server_conn.sock,
                                server_hostname=self.hostname)
                    except AttributeError:
                        try:
                            self._remote_server_conn.sock = ssl.wrap_socket(
                                    self._remote_server_conn.sock)
                        except ssl.SSLError:
                            self.logger.warning(f"failed to establish ssl connection to {self.hostname}; python ssl library does not support SNI,consider upgrading to python 2.7.9+ or 3.4+")
                        raise
                    except ssl.SSLError as e:
                        self.logger.error(f'error connecting to {self.hostname} ({remote_ip}) port {self.port}: {e}')
                        raise
            return self._remote_server_conn.sock
        MitmProxyHandler._connect_to_remote_server = _connect_to_remote_server

        # connect warcprox to an open port
        warcprox_port = 27500
        for i in range(500):
            try:
                options = warcprox.Options(
                    address="127.0.0.1",
                    port=warcprox_port,
                    max_threads=settings.MAX_PROXY_THREADS,
                    queue_size=settings.MAX_PROXY_QUEUE_SIZE,
                    gzip=True,
                    stats_db_file="",
                    dedup_db_file="",
                    directory="./warcs", # default, included so we can retrieve from options object
                    warc_filename=link.guid,
                    cacert=os.path.join(settings.PROJECT_ROOT, 'perma-warcprox-ca.pem'),
                    onion_tor_socks_proxy=settings.PROXY_ADDRESS if proxy else None
                )
                warcprox_controller = WarcproxController(options)
                break
            except socket_error as e:
                if e.errno != errno.EADDRINUSE:
                    raise
            warcprox_port += 1
        else:
            raise Exception("WarcProx couldn't find an open port.")
        proxy_address = f"127.0.0.1:{warcprox_port}"

        # start warcprox in the background
        warcprox_thread = threading.Thread(target=warcprox_controller.run_until_shutdown, name="warcprox", args=())
        warcprox_thread.start()
        print("WarcProx opened.")
        # END WARCPROX SETUP

        browser, display = get_browser(capture_user_agent, proxy_address, warcprox_controller.proxy.ca.ca_file)
        browser.set_window_size(*BROWSER_SIZE)

        print("Tracking capture size...")
        add_thread(thread_list, CaptureCurrentSizeThread(thread_list, proxied_responses))

        # fetch page in the background
        inc_progress(capture_job, 1, "Fetching target URL")
        page_load_thread = threading.Thread(target=browser.get, name="page_load", args=(target_url,))  # returns after onload
        page_load_thread.start()

        # before proceeding further, wait until warcprox records a response that isn't a forward
        with browser_running(browser):
            while not have_content:
                if proxied_responses["any"]:
                    for request, response in proxied_pairs:
                        if response is None:
                            # wait for the first response to finish, so we have the best chance
                            # at successfully identifying the content-type of the target_url
                            # (in unusual circumstances, can be incorrect)
                            break
                        if response.url.endswith(b'/favicon.ico') and response.url != target_url:
                            continue
                        if not hasattr(response, 'parsed_headers'):
                            response.parsed_headers = parse_headers(response.response_recorder.headers)
                        if response.status in [301, 302, 303, 307, 308, 206]:  # redirect or partial content
                            continue

                        have_content = True
                        content_url = str(response.url, 'utf-8')
                        content_type = getattr(response, 'content_type', None)
                        content_type = content_type.lower() if content_type else 'text/html; charset=utf-8'
                        robots_directives = response.parsed_headers.get('x-robots-tag')
                        have_html = content_type and content_type.startswith('text/html')
                        break

                if have_content:
                    # we have something that's worth showing to the user;
                    # break out of "while" before running sleep code below
                    break

                wait_time = time.time() - start_time
                if wait_time > RESOURCE_LOAD_TIMEOUT:
                    raise HaltCaptureException

                inc_progress(capture_job, wait_time/RESOURCE_LOAD_TIMEOUT, "Fetching target URL")
                time.sleep(1)

        print("Fetching robots.txt ...")
        add_thread(thread_list, robots_txt_thread, args=(
            link,
            target_url,
            content_url,
            thread_list,
            proxy_address,
            requested_urls,
            proxied_responses,
            capture_user_agent
        ))

        inc_progress(capture_job, 1, "Checking x-robots-tag directives.")
        if xrobots_blacklists_perma(robots_directives):
            safe_save_fields(link, is_private=True, private_reason='policy')
            print("x-robots-tag found, darchiving")

        if have_html:

            # Get a copy of the page's metadata immediately, without
            # waiting for the page's onload event (which can take a
            # long time, and might even crash the browser)
            print("Retrieving DOM (pre-onload)")
            dom_tree = get_dom_tree(browser)
            get_metadata(page_metadata, dom_tree)

            # get favicon urls (saved as favicon_capture_url later)
            with browser_running(browser):
                print("Fetching favicons ...")
                add_thread(thread_list, favicon_thread, args=(
                    successful_favicon_urls,
                    dom_tree,
                    content_url,
                    thread_list,
                    proxy_address,
                    requested_urls,
                    proxied_responses,
                    capture_user_agent
                ))

            print("Waiting for onload event before proceeding.")
            page_load_thread.join(max(0, ONLOAD_EVENT_TIMEOUT - (time.time() - start_time)))
            if page_load_thread.is_alive():
                print("Onload timed out")
            with browser_running(browser):
                try:
                    post_load_function = get_post_load_function(browser.current_url)
                except (WebDriverException, TimeoutException, CannotSendRequest):
                    post_load_function = get_post_load_function(content_url)
                if post_load_function:
                    print("Running domain's post-load function")
                    post_load_function(browser)

            # Get a fresh copy of the page's metadata, if possible.
            print("Retrieving DOM (post-onload)")
            dom_tree = get_dom_tree(browser)
            get_metadata(page_metadata, dom_tree)

            with browser_running(browser):
                inc_progress(capture_job, 0.5, "Checking for scroll-loaded assets")
                repeat_while_exception(scroll_browser, arglist=[browser], raise_after_timeout=False)

            inc_progress(capture_job, 1, "Fetching media")
            with warn_on_exception("Error fetching media"):
                dom_trees = get_all_dom_trees(browser)
                media_urls = get_media_tags(dom_trees)
                # grab all media urls that aren't already being grabbed,
                # each in its own background thread
                for media_url in media_urls - requested_urls:
                    add_thread(thread_list, ProxiedRequestThread(proxy_address, media_url, requested_urls, proxied_responses, capture_user_agent))

        # Wait AFTER_LOAD_TIMEOUT seconds for any requests that are started shortly to finish
        inc_progress(capture_job, 1, "Waiting for post-load requests")
        # everything is slower via the proxy; give it time to catch up
        if proxy:
            time.sleep(settings.PROXY_POST_LOAD_DELAY)
        unfinished_proxied_pairs = [pair for pair in proxied_pairs if not pair[1]]
        load_time = time.time()
        with browser_running(browser):
            while unfinished_proxied_pairs and browser_still_running(browser):

                if proxied_responses["limit_reached"]:
                    stop = True
                    print("Size limit reached: not waiting for additional pending requests.")
                    break

                print(f"Waiting for {len(unfinished_proxied_pairs)} pending requests")
                # give up after AFTER_LOAD_TIMEOUT seconds
                wait_time = time.time() - load_time
                if wait_time > AFTER_LOAD_TIMEOUT:
                    stop = True
                    print(f"Waited {AFTER_LOAD_TIMEOUT} seconds to finish post-load requests -- giving up.")
                    break

                # Show progress to user
                inc_progress(capture_job, wait_time/AFTER_LOAD_TIMEOUT, "Waiting for post-load requests")

                # Sleep and update our list
                time.sleep(.5)
                unfinished_proxied_pairs = [pair for pair in unfinished_proxied_pairs if not pair[1]]

        # screenshot capture of html pages (not pdf, etc.)
        # (after all requests have loaded for best quality)
        if have_html and browser_still_running(browser):
            inc_progress(capture_job, 1, "Taking screenshot")
            screenshot = get_screenshot(link, browser)
        else:
            safe_save_fields(link.screenshot_capture, status='failed')

    except HaltCaptureException:
        print("HaltCaptureException thrown")
    except SoftTimeLimitExceeded:
        capture_job.link.tags.add('timeout-failure')
    except:  # noqa
        logger.exception(f"Exception while capturing job {capture_job.link_id}:")
    finally:
        try:
            teardown(link, thread_list, browser, display, warcprox_controller, warcprox_thread)

            # save page metadata
            if have_html:
                if page_metadata:
                    process_metadata(page_metadata, link)
                else:
                    meta_tag_analysis_failed(link)

            if have_content:
                inc_progress(capture_job, 1, "Saving web archive file")
                save_warc(warcprox_controller, capture_job, link, content_type, screenshot, successful_favicon_urls)
                print(f"{link.guid} capture succeeded.")
            else:
                print(f"{link.guid} capture failed.")


        except:  # noqa
            logger.exception(f"Exception while finishing job {capture_job.link_id}:")
        finally:
            capture_job.link.captures.filter(status='pending').update(status='failed')
            if capture_job.status == 'in_progress':
                capture_job.mark_failed('Failed during capture.')
    if not os.path.exists(settings.DEPLOYMENT_SENTINEL):
        run_next_capture.delay()
    else:
        logger.info("Deployment sentinel is present, not running next capture.")


###              ###
### HOUSEKEEPING ###
###              ###

@shared_task()
def update_stats():
    """
    run once per minute by celerybeat. logs our minute-by-minute activity,
    and also rolls our weekly stats (perma.models.WeekStats)
    """

    # On the first minute of the new week, roll our weekly stats entry
    now = timezone.now()
    if now.weekday() == 6 and now.hour == 0 and now.minute == 0:
        week_to_close = WeekStats.objects.latest('start_date')
        week_to_close.end_date = now
        week_to_close.save()
        new_week = WeekStats(start_date=now)
        new_week.save()


    # We only need to keep a day of data for our visualization.
    # TODO: this is 1560 minutes is 26 hours, that likely doesn't
    # cover everyone outside of the east coast. Our vis should
    # be timezone aware. Fix this.
    if MinuteStats.objects.all().count() == 1560:
        MinuteStats.objects.all()[0].delete()


    # Add our new minute measurements
    a_minute_ago = now - timedelta(seconds=60)

    links_sum = Link.objects.filter(creation_timestamp__gt=a_minute_ago).count()
    users_sum = LinkUser.objects.filter(date_joined__gt=a_minute_ago).count()
    organizations_sum = Organization.objects.filter(date_created__gt=a_minute_ago).count()
    registrars_sum = Registrar.objects.approved().filter(date_created__gt=a_minute_ago).count()

    new_minute_stat = MinuteStats(links_sum=links_sum, users_sum=users_sum,
        organizations_sum=organizations_sum, registrars_sum=registrars_sum)
    new_minute_stat.save()


    # Add our minute activity to our current weekly sum
    if links_sum or users_sum or organizations_sum or registrars_sum:
        current_week = WeekStats.objects.latest('start_date')
        current_week.end_date = now
        current_week.links_sum += links_sum
        current_week.users_sum += users_sum
        current_week.organizations_sum += organizations_sum
        current_week.registrars_sum += registrars_sum
        current_week.save()


@shared_task(acks_late=True)  # use acks_late for tasks that can be safely re-run if they fail
def cache_playback_status_for_new_links():
    links = Link.objects.permanent().filter(cached_can_play_back__isnull=True)
    queued = 0
    for link_guid in links.values_list('guid', flat=True).iterator():
        cache_playback_status.delay(link_guid)
        queued = queued + 1
    logger.info(f"Queued {queued} links to have their playback status cached.")


@shared_task(acks_late=True)  # use acks_late for tasks that can be safely re-run if they fail
def cache_playback_status(link_guid):
    link = Link.objects.get(guid=link_guid)
    link.cached_can_play_back = link.can_play_back()
    if link.tracker.has_changed('cached_can_play_back'):
        link.save(update_fields=['cached_can_play_back'])


@shared_task()
def send_js_errors():
    """
    finds all uncaught JS errors recorded in the last week, sends a report if errors exist
    """
    errors = UncaughtError.objects.filter(
        created_at__gte=timezone.now() - timedelta(days=7),
        resolved=False)

    if errors:
        formatted_errors = [err.format_for_reading() for err in errors]
        send_self_email("Uncaught Javascript errors",
                         HttpRequest(),
                         'email/admin/js_errors.txt',
                         {'errors': formatted_errors})
        return errors


@shared_task()
def sync_subscriptions_from_perma_payments():
    """
    Perma only learns about changes to a customer's record in Perma
    Payments when the user transacts with Perma. For admin convenience,
    refresh Perma's records on demand.
    """
    customers = LinkUser.objects.filter(in_trial=False)
    for customer in customers:
        try:
            customer.get_subscription()
        except PermaPaymentsCommunicationException:
            # This gets logged inside get_subscription; don't duplicate logging here
            pass


@shared_task(acks_late=True)
def populate_warc_size_fields(limit=None):
    """
    One-time task, to populate the warc_size field for links where we missed it, the first time around.
    See https://github.com/harvard-lil/perma/issues/2617 and https://github.com/harvard-lil/perma/issues/2172;
    old links also often lack this metadata.
    """
    links = Link.objects.filter(warc_size__isnull=True, cached_can_play_back=True)
    if limit:
        links = links[:limit]
    queued = 0
    for link_guid in links.values_list('guid', flat=True).iterator():
        populate_warc_size.delay(link_guid)
        queued = queued + 1
    logger.info(f"Queued {queued} links for populating warc_size.")


@shared_task(acks_late=True)
def populate_warc_size(link_guid):
    """
    One-time task, to populate the warc_size field for links where we missed it, the first time around.
    See https://github.com/harvard-lil/perma/issues/2617 and https://github.com/harvard-lil/perma/issues/2172;
    old links also often lack this metadata.
    """
    link = Link.objects.get(guid=link_guid)
    link.warc_size = default_storage.size(link.warc_storage_file())
    link.save(update_fields=['warc_size'])


###                  ###
### INTERNET ARCHIVE ###
###                  ###

CONNECTION_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.HTTPError,
    requests.exceptions.ReadTimeout
)

def queue_batched_tasks(task, query, batch_size=1000, **kwargs):
    """
    A generic queuing task. Chunks the queryset by batch_size,
    and queues up the specified celery task for each chunk, passing in a
    list of the objects' primary keys and any other supplied kwargs.
    """
    query = query.values_list('pk', flat=True)

    first = None
    last = None
    batches_queued = 0
    pks = []
    for pk in query.iterator():

        # track the first pk for logging
        if not first:
            first = pk

        pks.append(pk)
        if len(pks) >= batch_size:
            task.delay(pks, **kwargs)
            batches_queued = batches_queued + 1
            pks = []

        # track the last pk for logging
        last = pk

    remainder = len(pks)
    if remainder:
        task.delay(pks, **kwargs)
        last = pks[-1]

    logger.info(f"Queued {batches_queued} batches of size {batch_size}{' and a single batch of size ' + str(remainder) if remainder else ''}, pks {first}-{last}.")


@shared_task(acks_late=True)
def upload_link_to_internet_archive(link_guid, attempts=0, timeouts=0):
    """
    This task adds a link's WARC and metadata to a "daily" Internet Archive item
    using IA's S3-like API. If it fails, it re-queues itself up to settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT,
    settings.INTERNET_ARCHIVE_UPLOAD_MAX_TIMEOUTS, or settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT times.
    """

    # Get the link and verify that it is eligible for upload
    link = Link.objects.get(guid=link_guid)
    if not link.can_upload_to_internet_archive():
        logger.info(f"Queued Link {link_guid} no longer eligible for upload.")
        return

    # Get or create the appropriate InternetArchiveItem object for this link
    date_string = link.creation_timestamp.strftime('%Y-%m-%d')
    identifier = InternetArchiveItem.DAILY_IDENTIFIER.format(
        prefix=settings.INTERNET_ARCHIVE_DAILY_IDENTIFIER_PREFIX,
        date_string=date_string
    )
    start = InternetArchiveItem.datetime(f"{date_string} 00:00:00")
    end = start + timedelta(days=1)
    perma_item, _created = InternetArchiveItem.objects.get_or_create(
        identifier=identifier,
        span=(start, end),
    )

    # Check the Internet Archive-related status of this link
    perma_file = InternetArchiveFile.objects.filter(
        item_id=identifier,
        link_id=link_guid
    ).first()
    if perma_file:
        if perma_file.status == 'confirmed_present':
            logger.info(f"Not uploading {link_guid} to {identifier}: our records indicate it is already present.")
            return
        elif perma_file.status in ['deletion_attempted', 'deletion_submitted']:
            # If we find ourselves here, something has gotten very mixed up indeed. We probably need a human to have a look.
            # Use the error log, assuming this will happen rarely or never.
            logger.error(f"Please investigate the status of {link_guid}: our records indicate a deletion attempt is in progress, but an upload was attempted in the meantime.")
            return
        elif perma_file.status in ['upload_attempted', 'upload_submitted']:
            logger.info(f"Potentially redundant attempt to upload {link_guid} to {identifier}: if this message recurs, please look into its status.")
        elif perma_file.status == 'confirmed_absent':
            logger.info(f"Uploading {link_guid} (previously deleted) to {identifier}.")
        else:
            logger.warning(f"Not uploading {link_guid} to {identifier}: task not implemented for InternetArchiveFiles with status '{perma_file.status}'.")
            return
    else:
        # A fresh one. Create the InternetArchiveFile here.
        perma_file = InternetArchiveFile(
            item_id=identifier,
            link_id=link_guid,
            status='upload_attempted'
        )
        perma_file.save()
        logger.info(f"Uploading {link_guid} to {identifier}.")


    # Attempt the upload

    def retry_upload(attempt_count, timeout_count):
        perma_item.tasks_in_progress = F('tasks_in_progress') - 1
        perma_item.save(update_fields=['tasks_in_progress'])
        upload_link_to_internet_archive.delay(link_guid, attempt_count, timeout_count)

    # Indicate that this InternetArchiveItem should be tracked until further notice
    perma_item.tasks_in_progress = F('tasks_in_progress') + 1
    perma_item.save(update_fields=['tasks_in_progress'])

    # Record that we are attempting an upload
    perma_file.status = 'upload_attempted'
    perma_file.save(update_fields=['status'])

    # Make sure we aren't exceeding rate limits
    ia_session = get_ia_session()
    s3_is_overloaded, s3_details = ia_session.get_s3_load_info(
        identifier=identifier,
        access_key=settings.INTERNET_ARCHIVE_ACCESS_KEY
    )
    perma_task_limit_approaching = ia_perma_task_limit_approaching(s3_details)
    global_task_limit_approaching = ia_global_task_limit_approaching(s3_details)
    bucket_task_limit_approaching = ia_bucket_task_limit_approaching(s3_details)
    if s3_is_overloaded or perma_task_limit_approaching or global_task_limit_approaching or bucket_task_limit_approaching:
        # This logging is noisy: we're not sure whether we want it or not, going forward.
        logger.warning(f"Skipped IA upload task for {link_guid} (IA Item {identifier}) due to rate limit: {s3_details}.")
        retry = (
            not settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT or
            (settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT > attempts + 1)
        )
        if retry:
            retry_upload(attempts + 1, timeouts)
        else:
            msg = f"Not retrying IA upload task for {link_guid} (IA Item {identifier}): rate limit retry maximum reached."
            if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                logger.exception(msg)
            else:
                logger.warning(msg)
        return

    # Get the IA Item
    try:
        ia_item = ia_session.get_item(identifier)
    except CONNECTION_ERRORS:
        # Sometimes, requests to retrieve the metadata of an IA Item time out.
        # Retry later, without counting this as a failed attempt
        logger.info(f"Re-queued 'upload_link_to_internet_archive' for {link_guid} after a connection error.")
        retry_upload(attempts, timeouts)
        return

    # Attempt the upload
    try:

        # mode set to 'ab+' as a workaround for https://bugs.python.org/issue25341
        with tempfile.TemporaryFile('ab+') as temp_warc_file:

            # copy warc to local disk storage for upload.
            # (potentially not necessary, but we think more robust against network conditions
            # https://github.com/harvard-lil/perma/commit/25eb14ce634675ffe67d0f14f51308f1202b53ea)
            with default_storage.open(link.warc_storage_file()) as warc_file:
                logger.info(f"Downloading {link.warc_storage_file()} from S3.")
                copy_file_data(warc_file, temp_warc_file)
                temp_warc_file.seek(0)

            response = ia_item.upload_file(
                body=temp_warc_file,
                key=InternetArchiveFile.WARC_FILENAME.format(guid=link_guid),
                metadata=InternetArchiveItem.standard_metadata_for_date(date_string),
                file_metadata=InternetArchiveFile.standard_metadata_for_link(link),
                access_key=settings.INTERNET_ARCHIVE_ACCESS_KEY,
                secret_key=settings.INTERNET_ARCHIVE_SECRET_KEY,
                queue_derive=False,
                retries=0,
                retries_sleep=0,
                verbose=False,
                debug=False,
            )
            assert response.status_code == 200, f"IA returned {response.status_code}): {response.text}"
    except SoftTimeLimitExceeded:
        retry = (
            not settings.INTERNET_ARCHIVE_UPLOAD_MAX_TIMEOUTS or
            (settings.INTERNET_ARCHIVE_UPLOAD_MAX_TIMEOUTS > timeouts + 1)
        )
        if retry:
            logger.info(f"Re-queued 'upload_link_to_internet_archive' for {link_guid} after SoftTimeLimitExceeded.")
            retry_upload(attempts, timeouts + 1)
        else:
            msg = f"Not retrying IA upload task for {link_guid} (IA Item {identifier}): timeout retry maximum reached."
            if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                logger.exception(msg)
            else:
                logger.warning(msg)
        return

    except CONNECTION_ERRORS:
        # If Internet Archive is unavailable, retry later, without counting this as a failed attempt.
        logger.info(f"Re-queued 'upload_link_to_internet_archive' for {link_guid} after a connection error.")
        retry_upload(attempts, timeouts)
        return

    except (requests.exceptions.HTTPError, AssertionError) as e:
        # upload_file internally calls response.raise_for_status, catching HTTPError
        # and re-raising with a custom error message.
        # https://github.com/jjjake/internetarchive/blob/a2de155d15aab279de6bb6364998266c21752ca6/internetarchive/item.py#L1048
        # https://github.com/jjjake/internetarchive/blob/a2de155d15aab279de6bb6364998266c21752ca6/internetarchive/item.py#L1073
        # ('InternalError', ('We encountered an internal error. Please try again.', '500 Internal Server Error'))
        # ('ServiceUnavailable', ('Please reduce your request rate.', '503 Service Unavailable'))
        # ('SlowDown', ('Please reduce your request rate.', '503 Slow Down'))
        error_string = str(e)
        if "Please reduce your request rate" in error_string:
            # This logging is noisy: we're not sure whether we want it or not, going forward.
            logger.warning(f"Upload task for {link_guid} (IA Item {identifier}) prevented by rate-limiting. Will retry if allowed.")
            retry = (
                not settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT or
                (settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT > attempts + 1)
            )
            if retry:
                retry_upload(attempts + 1, timeouts)
            else:
                msg = f"Not retrying IA upload task for {link_guid} (IA Item {identifier}): rate limit retry maximum reached."
                if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                    logger.exception(msg)
                else:
                    logger.warning(msg)
            return
        elif ("The bucket namespace is shared" in error_string or
              "Failed to get necessary short term bucket lock" in error_string or
              "auto_make_bucket requested" in error_string or
              ("Checking for identifier availability..." in error_string and "not_available" in error_string)):
            # These errors happen when we concurrently request to upload more than one file to an Item
            # that does not yet exist: each concurrent request attempts to create it, and is thwarted
            # by IA code guarding against inconsistent state. We need to support concurrent uploads
            # because of our volume. Since we cannot create the Item in an advance preparatory step
            # without a lot of engineering work on our end, we simply live with these errors, and
            # re-queue the failed attempts, without considering it a failed attempt.
            retry_upload(attempts, timeouts)
            return
        else:
            logger.warning(f"Upload task for {link_guid} (IA Item {identifier}) encountered an unexpected error ({ str(e).strip() }). Will retry if allowed.")
            retry = (
                not settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT or
                (settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT > attempts + 1)
            )
            if retry:
                retry_upload(attempts + 1, timeouts)
            else:
                msg = f"Not retrying IA upload task for {link_guid} (IA Item {identifier}, File {link.guid}): error retry maximum reached."
                if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                    logger.exception(msg)
                else:
                    logger.warning(msg)
            return

    # Record that the upload has been submitted
    perma_file.status = 'upload_submitted'
    perma_file.save(update_fields=['status'])

    logger.info(f"Uploaded {link_guid} to {identifier}: confirmation pending.")


@shared_task(acks_late=True)
def queue_file_uploaded_confirmation_tasks(limit=None):
    """
    It takes some time for IA to finish processing uploads, even after the S3-like API
    returns a success code. This task schedules a confirmation task for each file we've
    attempted to upload but have not yet verified has succeeded. We do this on a schedule,
    rather than immediately upon uploading a file, in order to introduce a delay: if we
    start checking immediately, an intolerable number of attempts fail... which causes
    too much IA API usage and too much churn.

    This may be too blunt an instrument; we may need to introduce a delay in the confirmation
    task itself, sleeping between each retry, but we want to try this first: if we can, we want
    to avoid having sleeping-but-active celery tasks.
    """
    tasks_in_ia_readonly_queue = redis.from_url(settings.CELERY_BROKER_URL).llen('ia-readonly')

    if not tasks_in_ia_readonly_queue:

        file_ids = InternetArchiveFile.objects.filter(
                    status='upload_submitted'
                ).exclude(
                    item_id__in=[
                        'daily_perma_cc_2022-07-25',
                        'daily_perma_cc_2022-07-21',
                        'daily_perma_cc_2022-07-20',
                        'daily_perma_cc_2022-07-19'
                    ]
                ).values_list(
                    'id', flat=True
                )[:limit]

        queued = 0
        for file_id in file_ids.iterator():
            confirm_file_uploaded_to_internet_archive.delay(file_id)
            queued = queued + 1
        logger.info(f"Queued the file upload confirmation task for {queued} InternetArchiveFiles.")

    else:
        logger.info(f"Skipped the queuing of file upload confirmation tasks: {tasks_in_ia_readonly_queue} task{pluralize(tasks_in_ia_readonly_queue)} in the ia-readonly queue.")

@shared_task(acks_late=True)
def confirm_file_uploaded_to_internet_archive(file_id, attempts=0, connection_errors=0):
    """
    This task checks to see if a WARC uploaded to IA's S3-like API has been processed
    and the new WARC is now visibly a part of the expected IA Item;
    if not, the task re-queues itself up to settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT times.
    Once the file is confirmed to be present, it marks that IA item needs to have its
    "derive.php" task re-triggered.
    """
    perma_file = InternetArchiveFile.objects.select_related('item', 'link').get(id=file_id)
    perma_item = perma_file.item
    link = perma_file.link

    if perma_file.status == 'confirmed_present':
        logger.info(f"InternetArchiveFile {file_id} ({link.guid}) already confirmed to be uploaded to {perma_item.identifier}.")
        return

    ia_session = get_ia_session()
    try:
        ia_item = ia_session.get_item(perma_item.identifier)
        ia_file = ia_item.get_file(InternetArchiveFile.WARC_FILENAME.format(guid=link.guid))
    except CONNECTION_ERRORS:
        # Sometimes, requests to retrieve the metadata of an IA Item time out. Retry later.
        if connection_errors < settings.INTERNET_ARCHIVE_RETRY_FOR_CONFIRMATION_CONNECTION_ERROR:
            confirm_file_uploaded_to_internet_archive.delay(file_id, attempts, connection_errors + 1)
            logger.info(f"Re-queued 'confirm_link_uploaded_to_internet_archive' for InternetArchiveFile {file_id} ({link.guid}) after a connection error.")
        return

    expected_metadata = InternetArchiveFile.standard_metadata_for_link(link)
    try:
        assert ia_file.exists
        for k, v in expected_metadata.items():
            # IA normalizes whitespace idiosyncratically:
            # ignore all whitespace when checking for expected values
            assert remove_whitespace(ia_file.metadata.get(k, '')) == remove_whitespace(v), f"expected {k}: {v}, got {ia_file.metadata.get(k)}."
    except AssertionError:
        # IA's tasks can take some time to complete;
        # the upload-related tasks for this link appear not to have finished yet.
        # We'll need to check again later, the next time celerybeat schedules these tasks.
        logger.info(f"Submitted upload of {link.guid} to IA Item {perma_item.identifier} not yet confirmed.")
        return

    # Update the InternetArchiveFile accordingly
    perma_file.update_from_ia_metadata(ia_file.metadata)
    perma_file.status = 'confirmed_present'
    perma_file.cached_size =  ia_file.size
    perma_file.save(update_fields=[
        'status',
        'cached_size',
        'cached_title',
        'cached_comments',
        'cached_external_identifier',
        'cached_external_identifier_match_date',
        'cached_format',
        'cached_submitted_url',
        'cached_perma_url'
    ])

    # If this is the first confirmed upload to this IA item,
    # cache its basic metadata locally
    if not perma_item.confirmed_exists:
        perma_item.confirmed_exists = True
        perma_item.added_date = InternetArchiveItem.datetime(ia_item.metadata['addeddate'])
        perma_item.cached_title = ia_item.metadata['title']
        perma_item.cached_description = ia_item.metadata.get('description')
        perma_item.save(update_fields=[
            'confirmed_exists',
            'added_date',
            'cached_title',
            'cached_description'
        ])

    # Update InternetArchiveItem accordingly
    perma_item.derive_required = True
    perma_item.cached_file_count = ia_item.files_count
    perma_item.tasks_in_progress = Greatest(F('tasks_in_progress') - 1, 0)
    perma_item.save(update_fields=[
        'derive_required',
        'cached_file_count',
        'tasks_in_progress'
    ])

    logger.info(f"Confirmed upload of {link.guid} to {perma_item.identifier}.")


@shared_task(acks_late=True)
def delete_link_from_daily_item(link_guid, attempts=0):
    perma_file = InternetArchiveFile.objects.select_related('item').get(link_id=link_guid, item__span__isempty=False)
    perma_item = perma_file.item
    identifier = perma_item.identifier

    def retry_deletion(attempt_count):
        perma_item.tasks_in_progress = F('tasks_in_progress') - 1
        perma_item.save(update_fields=['tasks_in_progress'])
        delete_link_from_daily_item.delay(link_guid, attempt_count)

    if perma_file.status == 'confirmed_absent':
        logger.info(f"The daily InternetArchiveFile for {link_guid} is already confirmed absent from {identifier}.")
        return
    elif perma_file.status in ['upload_attempted', 'upload_submitted']:
        # If we find ourselves here, something has gotten very mixed up indeed. We probably need a human to have a look.
        # Use the error log, assuming this will happen rarely or never.
        logger.error(f"Please investigate the status of {link_guid}: our records indicate an upload attempt is in progress, but a deletion was attempted in the meantime.")
        return
    elif perma_file.status in ['deletion_attempted', 'deletion_submitted']:
        logger.info(f"Potentially redundant attempt to delete {link_guid} from {identifier}: if this message recurs, please look into its status.")
    elif perma_file.status == 'confirmed_present':
        logger.info(f"Deleting {link_guid} from {identifier}.")
    else:
        logger.warning(f"Not deleting {link_guid} from {identifier}: task not implemented for InternetArchiveFiles with status '{perma_file.status}'.")
        return

    # Record that we are attempting a deletion
    perma_file.status = 'deletion_attempted'
    perma_file.save(update_fields=['status'])

    # Indicate that this InternetArchiveItem should be tracked until further notice
    perma_item.tasks_in_progress = F('tasks_in_progress') + 1
    perma_item.save(update_fields=['tasks_in_progress'])

    # Make sure we aren't exceeding rate limits
    ia_session = get_ia_session()
    s3_is_overloaded, s3_details = ia_session.get_s3_load_info(
        identifier=identifier,
        access_key=settings.INTERNET_ARCHIVE_ACCESS_KEY
    )
    perma_task_limit_approaching = ia_perma_task_limit_approaching(s3_details)
    global_task_limit_approaching = ia_global_task_limit_approaching(s3_details)
    if s3_is_overloaded or perma_task_limit_approaching or global_task_limit_approaching:
        # This logging is noisy: we're not sure whether we want it or not, going forward.
        logger.warning(f"Skipped IA deletion task for {link_guid} (IA Item {identifier}) due to rate limit: {s3_details}.")
        retry = (
            not settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT or
            (settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT > attempts + 1)
        )
        if retry:
            retry_deletion(attempts + 1)
        else:
            msg = f"Not retrying IA deletion task for {link_guid} (IA Item {identifier}): rate limit retry maximum reached."
            if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                logger.exception(msg)
            else:
                logger.warning(msg)
        return

    # Get the IA Item and File
    try:
        ia_item = ia_session.get_item(identifier)
        ia_file = ia_item.get_file(InternetArchiveFile.WARC_FILENAME.format(guid=link_guid))
    except CONNECTION_ERRORS:
        # Sometimes, requests to retrieve the metadata of an IA Item time out.
        # Retry later, without counting this as a failed attempt
        logger.info(f"Re-queued 'delete_link_from_daily_item' for {link_guid} after a connection error.")
        retry_deletion(attempts)
        return

    # attempt the deletion
    try:
        response = ia_file.delete(
            cascade_delete=False,  # is this correct? not sure: test with "derived" items
            access_key=settings.INTERNET_ARCHIVE_ACCESS_KEY,
            secret_key=settings.INTERNET_ARCHIVE_SECRET_KEY,
            verbose=False,
            debug=False,
            retries=0,
        )
        assert response.status_code == 204, f"IA returned {response.status_code}): {response.text}"
    except (requests.exceptions.RetryError,
            requests.exceptions.HTTPError,
            OSError,
            AssertionError) + CONNECTION_ERRORS as e:
        # `delete` internally calls response.raise_for_status catches and re-raises these exceptions.
        # https://github.com/jjjake/internetarchive/blob/a2de155d15aab279de6bb6364998266c21752ca6/internetarchive/files.py#L354
        # I am not sure why that is a longer list than the `upload` code catches.
        # It could be that we should catch the longer list in both places.
        # I am anticipating that the rate-limiting related error messages will be identical here,
        # but do not know if that is in fact the case.
        # ('InternalError', ('We encountered an internal error. Please try again.', '500 Internal Server Error'))
        # ('ServiceUnavailable', ('Please reduce your request rate.', '503 Service Unavailable'))
        # ('SlowDown', ('Please reduce your request rate.', '503 Slow Down'))
        if "Please reduce your request rate" in str(e):
            # This logging is noisy: we're not sure whether we want it or not, going forward.
            logger.warning(f"Deletion task for {link_guid} (IA Item {identifier}) prevented by rate-limiting. Will retry if allowed.")
            retry = (
                not settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT or
                (settings.INTERNET_ARCHIVE_RETRY_FOR_RATELIMITING_LIMIT > attempts + 1)
            )
            if retry:
                retry_deletion(attempts + 1)
            else:
                msg = f"Not retrying IA deletion task for {link_guid} (IA Item {identifier}): rate limit retry maximum reached."
                if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                    logger.exception(msg)
                else:
                    logger.warning(msg)
            return
        else:
            logger.warning(f"Deletion task for {link_guid} (IA Item {identifier}) encountered an unexpected error ({ str(e).strip() }). Will retry if allowed.")
            retry = (
                not settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT or
                (settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT > attempts + 1)
            )
            if retry:
                retry_deletion(attempts + 1)
            else:
                msg = f"Not retrying IA deletion task for {link_guid} (IA Item {identifier}, File {link_guid}): error retry maximum reached."
                if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                    logger.exception(msg)
                else:
                    logger.warning(msg)
            return

    # Record that the deletion has been submitted
    perma_file.status = 'deletion_submitted'
    perma_file.save(update_fields=['status'])

    logger.info(f"Requested deletion of {link_guid} from {identifier}: confirmation pending.")


@shared_task(acks_late=True)
def confirm_file_deleted_from_daily_item(file_id, attempts=0, connection_errors=0):
    """
    This task checks to see if a file we have requested be deleted from internet archive
    is still visibly a part of the expected IA Item;
    if it is, the task re-queues itself up to settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT times.
    Once the file is confirmed to be absent, it marks that IA item needs to have its
    "derive.php" task re-triggered.
    """
    perma_file = InternetArchiveFile.objects.select_related('item').get(id=file_id)
    perma_item = perma_file.item
    guid = perma_file.link_id

    if perma_file.status == 'confirmed_absent':
        logger.info(f"InternetArchiveFile {file_id} ({guid}) already confirmed absent from {perma_item.identifier}.")
        return

    ia_session = get_ia_session()
    try:
        ia_item = ia_session.get_item(perma_item.identifier)
        ia_file = ia_item.get_file(InternetArchiveFile.WARC_FILENAME.format(guid=guid))
    except CONNECTION_ERRORS:
        # Sometimes, requests to retrieve the metadata of an IA Item time out. Retry later.
        if connection_errors < settings.INTERNET_ARCHIVE_RETRY_FOR_CONFIRMATION_CONNECTION_ERROR:
            confirm_file_deleted_from_daily_item.delay(file_id, attempts, connection_errors + 1)
            logger.info(f"Re-queued 'confirm_file_deleted_from_daily_item' for InternetArchiveFile {file_id} ({guid}) after a connection error.")
        return

    try:
        assert not ia_file.exists
    except AssertionError:
        # IA's tasks can take some time to complete;
        # the deletion-related tasks for this link appear not to have finished yet.
        # We need to check again later.
        retry = (
            not settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT or
            (settings.INTERNET_ARCHIVE_RETRY_FOR_ERROR_LIMIT > attempts + 1)
        )
        if retry:
            confirm_file_deleted_from_daily_item.delay(file_id, attempts + 1)
            logger.info(f"Re-queued 'confirm_file_deleted_from_daily_item' for InternetArchiveFile {file_id} ({guid}).")
        else:
            msg = f"Not retrying 'confirm_file_deleted_from_daily_item' for {file_id} (IA Item {perma_item.identifier}, File {guid}): error retry maximum reached."
            if settings.INTERNET_ARCHIVE_EXCEPTION_IF_RETRIES_EXCEEDED:
                logger.exception(msg)
            else:
                logger.warning(msg)
        return

    # Update the InternetArchiveFile accordingly
    perma_file.status = 'confirmed_absent'
    perma_file.zero_cached_ia_metadata()
    perma_file.save(update_fields=[
        'status',
        'cached_size',
        'cached_title',
        'cached_comments',
        'cached_external_identifier',
        'cached_external_identifier_match_date',
        'cached_format',
        'cached_submitted_url',
        'cached_perma_url'
    ])

    # Update InternetArchiveItem accordingly
    perma_item.derive_required = True
    perma_item.cached_file_count = ia_item.files_count
    perma_item.tasks_in_progress = Greatest(F('tasks_in_progress') - 1, 0)
    perma_item.save(update_fields=[
        'derive_required',
        'cached_file_count',
        'tasks_in_progress'
    ])

    logger.info(f"Confirmed deletion of {guid} from {perma_item.identifier}.")


@shared_task(acks_late=True)
def queue_file_deleted_confirmation_tasks(limit=100):
    """
    It takes some time for IA to finish processing deletions, even after the S3-like API
    returns a success code. This task schedules a confirmation task for each file we've
    attempted to delete but have not yet verified has succeeded. We do this on a schedule,
    rather than immediately upon uploading a file, in order to introduce a delay: if we
    start checking immediately, an intolerable number of attempts fail... which causes
    too much IA API usage and too much churn.

    This may be too blunt an instrument; we may need to introduce a delay in the confirmation
    task itself, sleeping between each retry, but we want to try this first: if we can, we want
    to avoid having sleeping-but-active celery tasks.
    """
    tasks_in_ia_readonly_queue = redis.from_url(settings.CELERY_BROKER_URL).llen('ia-readonly')

    if not tasks_in_ia_readonly_queue:

        file_ids = InternetArchiveFile.objects.filter(
                    status='deletion_submitted'
                ).exclude(
                    item_id__in=[
                        'daily_perma_cc_2022-07-25',
                        'daily_perma_cc_2022-07-21',
                        'daily_perma_cc_2022-07-20',
                        'daily_perma_cc_2022-07-19'
                    ]
                ).values_list(
                    'id', flat=True
                )[:limit]

        queued = 0
        for file_id in file_ids.iterator():
            confirm_file_deleted_from_daily_item.delay(file_id)
            queued = queued + 1
        logger.info(f"Queued the file deleted confirmation task for {queued} InternetArchiveFiles.")

    else:
        logger.info(f"Skipped the queuing of file deleted confirmation tasks: {tasks_in_ia_readonly_queue} task{pluralize(tasks_in_ia_readonly_queue)} in the ia-readonly queue.")


@shared_task
def queue_internet_archive_deletions(limit=None):
    """
    Queue deletion tasks for any currently-ineligible Links that were eligible
    when daily IA items were initially created...and so were uploaded.

    (Don't limit by creation date: this is expected to be a small number.)
    """
    to_delete = Link.objects.ineligible_for_ia().filter(
        internet_archive_items__span__isempty=False,
        internet_archive_files__status__in=['confirmed_present', 'deletion_attempted']
    )[:limit]

    # Queue the tasks
    queued = []
    query_started = time.time()
    query_ended = None
    try:
        for guid in to_delete.values_list('guid', flat=True).iterator():
            if not query_ended:
                # log here: the query won't actually be evaluated until .iterator() is called
                query_ended = time.time()
                logger.info(f"Ready to queue links for deletion in {query_ended - query_started} seconds.")
            delete_link_from_daily_item.delay(guid)
            queued.append(guid)
    except SoftTimeLimitExceeded:
        pass

    logger.info(f"Queued { len(queued) } links for deletion ({queued[0]} through {queued[-1]}).")


def queue_internet_archive_uploads_for_date(date_string, limit=100):
    """
    Queue upload tasks for all currently-eligible Links created on a given day,
    if we have not yet attempted to upload them to a "daily" Item.
    """

    # force the query to evaluate so we can time it, and use a strategy that
    # lets us test whether any links were found and iterate through the queryset,
    # without needing to query the database twice
    query_started = time.time()
    to_upload = Link.objects.ia_upload_pending(date_string, limit).iterator()
    first_link_to_upload = next(to_upload, None)
    query_ended = time.time()

    if first_link_to_upload:
        logger.info(f"Ready to queue links for upload in {query_ended - query_started} seconds.")
        queued = []
        try:
            for link in itertools.chain([first_link_to_upload], to_upload):
                upload_link_to_internet_archive.delay(link.guid)
                queued.append(link.guid)
        except SoftTimeLimitExceeded:
            pass
        logger.info(f"Queued { len(queued) } links for upload ({queued[0]} through {queued[-1]}).")
        return len(queued)
    else:
        logger.info(f"Found no links to upload in {query_ended - query_started} seconds.")
        identifier = InternetArchiveItem.DAILY_IDENTIFIER.format(
            prefix=settings.INTERNET_ARCHIVE_DAILY_IDENTIFIER_PREFIX,
            date_string=date_string
        )
        try:
            item = InternetArchiveItem.objects.get(identifier=identifier)
            item.complete = True
            item.save(update_fields=['complete'])
            logger.info(f"Found no pending links: marked IA Item {item.identifier} complete.")
        except InternetArchiveItem.DoesNotExist:
            logger.info(f"Found no pending links for {date_string}.")
        return 0


@shared_task
def conditionally_queue_internet_archive_uploads_for_date_range(start_date_string, end_date_string, daily_limit=100, limit=None):
    """
    Queues up to settings.INTERNET_ARCHIVE_MAX_SIMULTANEOUS_UPLOADS links for upload to IA, spread over
    a number of days such that no more than `daily_limit` are ever queued for a particular day. May
    queue fewer links, if an explicit `limit` is passed in, or if:
    - there are pending tasks in the Celery queue
    - there are submitted-but-as-of-yet-unfinished upload requests being processed by IA
    - there are not enough qualifying links in the date range
    - there are not enough qualifying links in the date range, while respecting daily_limit
    """
    tasks_in_ia_queue = redis.from_url(settings.CELERY_BROKER_URL).llen('ia')
    if tasks_in_ia_queue:
        logger.info(f"Skipped the queuing of file upload tasks: {tasks_in_ia_queue} task{pluralize(tasks_in_ia_queue)} in the ia queue.")
        return

    if not start_date_string:
        # for now, start the day after the last 'complete' day
        start = InternetArchiveItem.objects.filter(complete=True).order_by('-span').first().span.lower.date() + timedelta(days=1)
        # once that completes, and once we have verified that an InternetArchiveItem has been created
        # for every day in the backlog, with no gaps, we should instead start with the oldest incomplete
        # day in the backlog:
        #
        # oldest_incomplete_daily_item_in_backlog = InternetArchiveItem.objects.filter(
        #       span__isempty=False,
        #       span__gt=('2021-11-10', '2021-11-11'),
        #       complete=False,
        # ).order_by('span').first()
        # start = oldest_incomplete_daily_item_in_backlog.span.lower.date()
    else:
        start = datetime.strptime(start_date_string, '%Y-%m-%d').date()
    if not end_date_string:
        end = datetime.now().date()
    else:
        end = datetime.strptime(end_date_string, '%Y-%m-%d').date()
    if start > end:
        logger.error(f"Invalid range: start={start} end={end}.")

    tasks_in_flight = InternetArchiveItem.inflight_task_count()
    max_to_queue = settings.INTERNET_ARCHIVE_MAX_SIMULTANEOUS_UPLOADS - tasks_in_flight
    to_queue = min(max_to_queue, limit) if limit else max_to_queue

    if to_queue:

        total_queued = 0
        queued = []
        for day in date_range(start, end, timedelta(days=1)):
            if total_queued < to_queue:
                date_string = day.strftime('%Y-%m-%d')
                if date_string in ['2022-07-25', '2022-07-21', '2022-07-20', '2022-07-19']:
                    # for now, skip these days: by accident, we don't presently have edit
                    # privileges for the IA Items with these identifiers
                    continue
                identifier = InternetArchiveItem.DAILY_IDENTIFIER.format(
                    prefix=settings.INTERNET_ARCHIVE_DAILY_IDENTIFIER_PREFIX,
                    date_string=date_string
                )
                try:
                    in_flight_for_this_day = InternetArchiveItem.objects.get(identifier=identifier).tasks_in_progress
                except InternetArchiveItem.DoesNotExist:
                    in_flight_for_this_day = 0
                bucket_limit = min(daily_limit, to_queue - total_queued) - in_flight_for_this_day
                if bucket_limit > 0:
                    count_queued = queue_internet_archive_uploads_for_date(date_string, bucket_limit)
                    if count_queued:
                        total_queued += count_queued
                        queued.append(f"{date_string} ({count_queued})")
            else:
                break

        logger.info(f"Prepared to upload {total_queued} links to internet archive across {len(queued)} days: {', '.join(queued)}.")

    else:
        logger.info("Skipped the queuing of file upload tasks: max tasks already in progress.")
