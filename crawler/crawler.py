"""
Created on 12.11.2014

@author: constantin
"""
import logging
import sys
from enum import Enum
from copy import deepcopy
from urllib.parse import urljoin

from PyQt5.Qt import QApplication, QObject

from analyzer.eventexecutor import EventExecutor, XHR_Behavior, Event_Result
from analyzer.formhandler import FormHandler
from database.persistentmanager import PersistenceManager
from models.clustermanager import ClusterManager
from models.url import Url
from utils.execptions import PageNotFoundException, LoginException
from models.deltapage import DeltaPage
from models.webpage import WebPage
from models.clickabletype import ClickableType
from utils.domainhandler import DomainHandler
from analyzer.mainanalyzer import MainAnalyzer
from network.network import NetWorkAccessManager
from utils.utils import calculate_similarity_between_pages, subtract_parent_from_delta_page, form_to_dict


potential_logout_urls = []


class Crawler(QObject):
    def __init__(self, crawl_config, proxy="", port=0, persistence_manager=None):
        QObject.__init__(self)
        self.app = QApplication(sys.argv)
        self._network_access_manager = NetWorkAccessManager(self)
        self._event_executor = EventExecutor(self, proxy, port, crawl_speed=crawl_config.crawl_speed,
                                             network_access_manager=self._network_access_manager)
        self._dynamic_analyzer = MainAnalyzer(self, proxy, port, crawl_speed=crawl_config.crawl_speed,
                                          network_access_manager=self._network_access_manager)
        self._form_handler = FormHandler(self, proxy, port, crawl_speed=crawl_config.crawl_speed,
                                             network_access_manager=self._network_access_manager)

        self.domain_handler = None
        self.current_depth = 0
        self.crawl_with_login = False
        self.proxy = proxy
        self.port = port

        self.crawler_state = CrawlState.NormalPage
        self.crawl_config = crawl_config
        self.tmp_delta_page_storage = []  # holds the deltapages for further analyses
        self.url_frontier = []
        self.user = None
        self.page_id = 0
        self.current_depth = 0
        self.persistence_manager = persistence_manager

        self.cluster_manager = ClusterManager(self.persistence_manager) # dict with url_hash and


    def crawl(self, user):
        self.user = user
        self.domain_handler = DomainHandler(self.crawl_config.start_page_url, self.persistence_manager)
        self.start_page_url = self.domain_handler._create_url(self.crawl_config.start_page_url, None)
        self.persistence_manager.insert_url_into_db(self.start_page_url)

        if self.user.login_data is not None:
            self.crawl_with_login = True
            go_on = self.initial_login(self.domain_handler._create_url(self.user.url_with_login_form), self.user.login_data)
            if not go_on:
                raise LoginException("Initial login failed...")

        necessary_clicks = []  # Saves the actions the crawler need to reach a delta page
        parent_page = None  # Saves the parent of the delta-page (not other delta pages)
        previous_pages = []  # Saves all the pages the crawler have to pass to reach my delta-page

        logging.debug("Crawl with userId: " + str(self.user.username))

        while True:
            logging.debug("=======================New Round=======================")
            parent_page = None
            current_page = None
            necessary_clicks = []
            previous_pages = []
            delta_page = None

            if len(self.tmp_delta_page_storage) > 0:
                self.crawler_state = CrawlState.DeltaPage
                current_page = self.tmp_delta_page_storage.pop(0)
                logging.debug("Processing Deltapage with ID: {}, {} deltapages left...".format(str(current_page.id),
                                                                                               str(len(
                                                                                                   self.tmp_delta_page_storage))))
                parent_page = current_page
                while isinstance(parent_page, DeltaPage):
                    necessary_clicks.insert(0,
                                            parent_page.generator)  # Insert as first element because of reverse order'
                    parent_page = self.persistence_manager.get_page_to_id(parent_page.parent_id)
                    if parent_page is None:
                        raise PageNotFoundException("This exception should never be raised...")
                    previous_pages.append(parent_page)
                # Now I'm reaching a non delta-page
                self.current_depth = parent_page.current_depth
                url_to_request = self.domain_handler._create_url(parent_page.url)

            else:
                url_to_request = self.domain_handler.get_next_url_for_crawling()
                if url_to_request is not None:
                    self.crawler_state = CrawlState.NormalPage
                    if url_to_request.depth_of_finding is None:
                        self.current_depth = 0
                        url_to_request.depth_of_finding = 0
                    else:
                        self.current_depth = url_to_request.depth_of_finding + 1
                else:
                    break

            if self.crawler_state == CrawlState.NormalPage:
                if not self.domain_handler.is_in_scope(
                        url_to_request) or url_to_request.depth_of_finding > self.crawl_config.max_depth:
                    logging.debug("Ignoring(Not in scope or max crawl depth reached)...: " + url_to_request.toString())
                    self.persistence_manager.visit_url(url_to_request, None, 000)
                    continue
                response_code, response_url, html_after_timeouts, new_clickables, forms, links, timemimg_requests = self._dynamic_analyzer.analyze(url_to_request, current_depth=self.current_depth)

                current_page = WebPage(self.get_next_page_id(), response_url, html_after_timeouts)
                current_page.timeming_requests = timemimg_requests
                current_page.clickables = new_clickables
                current_page.links = links
                current_page.forms = forms
                self.domain_handler.complete_urls(current_page)
                self.persistence_manager.store_web_page(current_page)
                if response_code == 200:
                    self.persistence_manager.visit_url(url_to_request, current_page.id, response_code)
                else:
                    self.persistence_manager.visit_url(url_to_request, current_page.id, response_code, response_url)
                self.domain_handler.extract_new_links_for_crawling(current_page, current_page.current_depth, current_page.url)
                #logging.debug(page.toString())

            if self.crawler_state == CrawlState.DeltaPage:
                current_page.html = parent_page.html  # Assigning html
                logging.debug("Now at Deltapage: " + str(current_page.id))
                self.persistence_manager.store_delta_page(current_page)
            # break

            clickable_to_process = deepcopy(current_page.clickables)
            clickable_to_process = self.edit_clickables_for_execution(clickable_to_process)
            clickables = []
            counter = 1  # Just a counter for displaying progress
            errors = 0  # Count the errors(Missing preclickable or target elements=
            retries = 0  # Count the retries
            max_retries_for_clicking = 5

            while len(clickable_to_process) > 0 and retries < max_retries_for_clicking:
                clickable = clickable_to_process.pop(0)
                if not self.should_execute_clickable(clickable):
                    clickable.clickable_type = ClickableType.Ignored_by_Crawler
                    self.persistence_manager.update_clickable(current_page.id, clickable)
                    #clickables.append(clickable)
                    continue
                logging.debug(
                    "Processing Clickable Number {} - {} left".format(str(counter), str(len(clickable_to_process))))
                counter += 1

                """
                If event is something like "onclick", take of the "on"
                """
                event = clickable.event
                if event[0:2] == "on":
                    event = event[2:]
                if clickable.clicked:
                    continue

                """
                If event is not supported, mark it so in the database and continue
                """
                if event not in self._event_executor.supported_events:
                    clickable.clickable_type = ClickableType.Unsuported_Event
                    self.persistence_manager.update_clickable(current_page.id, clickable)
                    clickables.append(clickable)
                    continue
                """
                Because I want first a run without sending something to the backend, I distinguish if I know an element or not.
                If I know it(its clickable_type is set) I re-execute the event and let the ajax request pass.
                If I don't know it, I execute each clickable with an interception.
                """
                if clickable.clickable_type is not None:
                    """
                    The clickable was executed in the past, and has triggered an backend request. Know execute it again and let that request pass
                    """
                    xhr_behavior = XHR_Behavior.observe_xhr
                    event_state, delta_page = self._event_executor.execute(current_page, element_to_click=clickable,
                                                                           pre_clicks=necessary_clicks,
                                                                           xhr_options=xhr_behavior)
                else:
                    """
                    The clickable was never executed, so execute it with intercepting all backend requests.
                    """
                    xhr_behavior = XHR_Behavior.intercept_xhr
                    event_state, delta_page = self._event_executor.execute(current_page, element_to_click=clickable,
                                                                           pre_clicks=necessary_clicks,
                                                                           xhr_options=xhr_behavior)

                if event_state == Event_Result.Unsupported_Tag:
                    clickable.clicked = True
                    clickable.clickable_type = ClickableType.Unsuported_Event
                    clickables.append(clickable)
                    self.persistence_manager.update_clickable(current_page.id, clickable)
                    continue

                if event_state == Event_Result.Target_Element_Not_Found or event_state == Event_Result.Error_While_Initial_Loading:
                    clickable.clicked = True
                    clickable.clickable_type = ClickableType.Error
                    clickables.append(clickable)
                    self.persistence_manager.update_clickable(current_page.id, clickable)
                    continue

                if event_state == Event_Result.Previous_Click_Not_Found:
                    errors += 1
                    clickable.clicked = False
                    error_ratio = errors / len(current_page.clickables)
                    if error_ratio > .2:
                        go_on = self.handling_possible_logout()
                        if not go_on:
                            # raise LoginException("Cannot login anymore")
                            continue
                        else:
                            retries += 1
                            errors = 0
                            clickable_to_process.append(clickable)
                            continue
                    else:
                        clickable_to_process.append(clickable)

                try:
                    delta_page.delta_depth = current_page.delta_depth + 1
                except AttributeError:
                    delta_page.delta_depth = 1

                if event_state == Event_Result.URL_Changed:
                    logging.debug("DeltaPage has new Url..." + delta_page.url)
                    clickable.clicked = True
                    clickable.links_to = delta_page.url
                    clickable.clickable_type = ClickableType.Link
                    new_url = self.domain_handler._create_url(delta_page.url,
                                                             depth_of_finding=current_page.current_depth)
                    self.persistence_manager.insert_url_into_db(new_url)
                    clickables.append(clickable)
                    self.persistence_manager.update_clickable(current_page.id, clickable)
                else:
                    """
                    Everything works fine and I get a normal DeltaPage, now I have to:
                        - Assigne the current depth to it -> DeltaPages have the same depth as its ParentPages
                        - Assign the cookies, just for output and debugging
                        - Analyze the Deltapage without addEventlisteners and timemimg check. This is done during event execution
                        - Substract the ParentPage, optional Parent + all previous visited DeltaPages, from the DeltaPage to get
                          get the real DeltaPage
                        - Handle it after the result of the substraction
                    """
                    clickable.clicked = True
                    clickable.clickable_depth = delta_page.delta_depth
                    delta_page.current_depth = self.current_depth
                    delta_page = self.domain_handler.complete_urls(delta_page)

                    if self.crawler_state == CrawlState.NormalPage:
                        delta_page = subtract_parent_from_delta_page(current_page, delta_page)
                    if self.crawler_state == CrawlState.DeltaPage:
                        delta_page = subtract_parent_from_delta_page(current_page, delta_page)
                        for p in previous_pages:
                            delta_page = subtract_parent_from_delta_page(p, delta_page)

                    if len(delta_page.clickables) > 0 or len(delta_page.links) > 0 or len(
                            delta_page.ajax_requests) > 0 or len(delta_page.forms) > 0:
                        if len(delta_page.links) != 0 and len(delta_page.ajax_requests) == 0 and len(
                                delta_page.clickables) == 0 and len(delta_page.forms) == 0:
                            clickable_process_again = self.handle_delta_page_has_only_new_links(clickable, delta_page, current_page,
                                                                                  xhr_behavior)

                        elif len(delta_page.links) == 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) == 0 and len(delta_page.forms) == 0:
                            clickable_process_again = self.handle_delta_page_has_only_ajax_requests(clickable, delta_page,
                                                                                      current_page, xhr_behavior)

                        elif len(delta_page.links) != 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) == 0 and len(delta_page.forms) == 0:
                            clickable_process_again = self.handle_delta_page_has_new_links_and_ajax_requests(clickable, delta_page,
                                                                                               current_page,
                                                                                               xhr_behavior)

                        elif len(delta_page.links) == 0 and len(delta_page.ajax_requests) == 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) == 0:
                            clickable_process_again = self.handle_delta_page_has_only_new_clickables(clickable, delta_page,
                                                                                       current_page, xhr_behavior)

                        elif len(delta_page.links) != 0 and len(delta_page.ajax_requests) == 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) == 0:
                            clicclickable_process_againkable = self.handle_delta_page_has_new_links_and_clickables(clickable, delta_page,
                                                                                            current_page, xhr_behavior)

                        elif len(delta_page.links) == 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) == 0:
                            clickable_process_again = self.handle_delta_page_has_new_clickables_and_ajax_requests(clickable,
                                                                                                    delta_page,
                                                                                                    current_page,
                                                                                                    xhr_behavior)

                        elif len(delta_page.links) != 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) == 0:
                            clickable_process_again = self.handle_delta_page_has_new_links_ajax_requests__clickables(clickable,
                                                                                                       delta_page,
                                                                                                       current_page,
                                                                                                       xhr_behavior)

                        elif len(delta_page.links) == 0 and len(delta_page.ajax_requests) == 0 and len(
                                delta_page.clickables) == 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_only_new_forms(clickable, delta_page, current_page,
                                                                                  xhr_behavior)

                        elif len(delta_page.links) != 0 and len(delta_page.ajax_requests) == 0 and len(
                                delta_page.clickables) == 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_new_links_and_forms(clickable, delta_page,
                                                                                       current_page, xhr_behavior)

                        elif len(delta_page.links) == 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) == 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_new_forms_and_ajax_requests(clickable, delta_page,
                                                                                               current_page,
                                                                                               xhr_behavior)

                        elif len(delta_page.links) != 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) == 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_new_links_forms_ajax_requests(clickable, delta_page,
                                                                                                 current_page,
                                                                                                 xhr_behavior)

                        elif len(delta_page.links) == 0 and len(delta_page.ajax_requests) == 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_new_clickable_and_forms(clickable, delta_page,
                                                                                           current_page, xhr_behavior)

                        elif len(delta_page.links) != 0 and len(delta_page.ajax_requests) == 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_new_links_clickables_forms(clickable, delta_page,
                                                                                              current_page,
                                                                                              xhr_behavior)

                        elif len(delta_page.links) == 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_new_clickables_forms_ajax_requests(clickable,
                                                                                                      delta_page,
                                                                                                      current_page,
                                                                                                      xhr_behavior)

                        elif len(delta_page.links) != 0 and len(delta_page.ajax_requests) != 0 and len(
                                delta_page.clickables) != 0 and len(delta_page.forms) != 0:
                            clickable_process_again = self.handle_delta_page_has_new_links_clickables_forms_ajax_requests(clickable,
                                                                                                            delta_page,
                                                                                                            current_page,
                                                                                                            xhr_behavior)

                        else:
                            logging.debug("Nothing matches...")
                            logging.debug("    Clickables: " + str(len(delta_page.clickables)))
                            logging.debug("    Links: " + str(len(delta_page.links)))
                            logging.debug("    Forms: " + str(len(delta_page.forms)))
                            logging.debug("    AjaxRequests: " + str(len(delta_page.ajax_requests)))

                        if clickable_process_again is not None:
                            clickable.clicked = False
                            clickable_to_process.append(clickable)
                        else:
                            clickables.append(clickable)

                    else:
                        clickable.clickable_type = ClickableType.UI_Change
                        self.persistence_manager.update_clickable(current_page.id, clickable)
                        clickables.append(clickable)

            current_page.clickables = clickables
            #self.print_to_file(current_page.toString(), str(current_page.id) + ".txt")4
            if self.crawler_state == CrawlState.NormalPage:
                self.cluster_manager.add_webpage_to_cluster(current_page)
        #self.cluster_manager.draw_clusters()
        logging.debug("Crawling is ready...")






    def handle_delta_page_has_only_new_links(self, clickable, delta_page, parent_page=None, xhr_behavior=None):
        delta_page.id = self.get_next_page_id()
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
        self.persistence_manager.store_delta_page(delta_page)
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        self.print_to_file(delta_page.toString(), str(delta_page.id) + ".txt")

    def handle_delta_page_has_only_new_clickables(self, clickable, delta_page, parent_page=None, xhr_behavior=None):
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        delta_page.id = self.get_next_page_id()
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        if self.should_delta_page_be_stored_for_crawling(delta_page):
            self._store_delta_page_for_crawling(delta_page)

    def handle_delta_page_has_only_new_forms(self, clickable, delta_page, parent_page=None, xhr_behavior=None):
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        delta_page.id = self.get_next_page_id()
        self.persistence_manager.store_delta_page(delta_page)
        self.domain_handler.extract_new_links_for_crawling(delta_page, self.current_depth)
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        self.print_to_file(delta_page.toString(), str(delta_page.id) + ".txt")

    def handle_delta_page_has_only_ajax_requests(self, clickable, delta_page, parent_page=None, xhr_behavior=None):
        self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
        clickable.clickable_type = ClickableType.SendingAjax
        if xhr_behavior == XHR_Behavior.observe_xhr:
            self.persistence_manager.extend_ajax_requests_to_webpage(parent_page, delta_page.ajax_requests)
        else:
            return clickable

    def handle_delta_page_has_new_links_and_clickables(self, clickable, delta_page, parent_page=None, xhr_behavior=None):
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        delta_page.id = self.get_next_page_id()
        self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        if self.should_delta_page_be_stored_for_crawling(delta_page):
            self._store_delta_page_for_crawling(delta_page)

    def handle_delta_page_has_new_links_and_forms(self, clickable, delta_page, parent_page=None, xhr_behavior=None):
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        delta_page.id = self.get_next_page_id()
        self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
        self.persistence_manager.store_delta_page(delta_page)
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        self.print_to_file(delta_page.toString(), str(delta_page.id) + ".txt")

    def handle_delta_page_has_new_links_and_ajax_requests(self, clickable, delta_page, parent_page=None,
                                                          xhr_behavior=None):
        if xhr_behavior == XHR_Behavior.observe_xhr:
            delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
            delta_page.id = self.get_next_page_id()
            self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
            delta_page.generator_requests.extend(delta_page.ajax_requests)
            delta_page.ajax_requests = []
            self.persistence_manager.store_delta_page(delta_page)
            self.persistence_manager.update_clickable(parent_page.id, clickable)
            self.print_to_file(delta_page.toString(), str(delta_page.id) + ".txt")
        else:
            clickable.clickable_type = ClickableType.SendingAjax
            return clickable

    def handle_delta_page_has_new_clickable_and_forms(self, clickable, delta_page, parent_page=None, xhr_behavior=None):
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        delta_page.id = self.get_next_page_id()
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        if self.should_delta_page_be_stored_for_crawling(delta_page):
            self._store_delta_page_for_crawling(delta_page)

    def handle_delta_page_has_new_clickables_and_ajax_requests(self, clickable, delta_page, parent_page=None,
                                                               xhr_behavior=None):
        if xhr_behavior == XHR_Behavior.observe_xhr:
            delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
            delta_page.id = self.get_next_page_id()
            self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
            delta_page.generator_requests.extend(delta_page.ajax_requests)
            delta_page.ajax_requests = []
            self.persistence_manager.update_clickable(parent_page.id, clickable)
            if self.should_delta_page_be_stored_for_crawling(delta_page):
                self._store_delta_page_for_crawling(delta_page)
        else:
            clickable.clickable_type = ClickableType.SendingAjax
            return clickable

    def handle_delta_page_has_new_forms_and_ajax_requests(self, clickable, delta_page, parent_page=None,
                                                          xhr_behavior=None):
        if xhr_behavior == XHR_Behavior.observe_xhr:
            delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
            delta_page.id = self.get_next_page_id()
            self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
            delta_page.generator_requests.extend(delta_page.ajax_requests)
            delta_page.ajax_requests = []
            self.persistence_manager.update_clickable(parent_page.id, clickable)
            if self.should_delta_page_be_stored_for_crawling(delta_page):
                self._store_delta_page_for_crawling(delta_page)
        else:
            clickable.clickable_type = ClickableType.SendingAjax
            return clickable

    def handle_delta_page_has_new_links_clickables_forms(self, clickable, delta_page, parent_page=None,
                                                         xhr_behavior=None):
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        delta_page.id = self.get_next_page_id()
        self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
        delta_page.generator_requests.extend(delta_page.ajax_requests)
        delta_page.ajax_requests = []
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        if self.should_delta_page_be_stored_for_crawling(delta_page):
            self._store_delta_page_for_crawling(delta_page)

    def handle_delta_page_has_new_links_forms_ajax_requests(self, clickable, delta_page, parent_page=None,
                                                            xhr_behavior=None):
        if xhr_behavior == XHR_Behavior.observe_xhr:
            delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
            delta_page.id = self.get_next_page_id()
            self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
            delta_page.generator_requests.extend(delta_page.ajax_requests)
            delta_page.ajax_requests = []
            self.persistence_manager.update_clickable(parent_page.id, clickable)
            if self.should_delta_page_be_stored_for_crawling(delta_page):
                self._store_delta_page_for_crawling(delta_page)
        else:
            clickable.clickable_type = ClickableType.SendingAjax
            return clickable

    def handle_delta_page_has_new_clickables_forms_ajax_requests(self, clickable, delta_page, parent_page=None,
                                                                 xhr_behavior=None):
        if xhr_behavior == XHR_Behavior.observe_xhr:
            delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
            self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
            delta_page.generator_requests.extend(delta_page.ajax_requests)
            delta_page.ajax_requests = []
            delta_page.id = self.get_next_page_id()
            self.persistence_manager.update_clickable(parent_page.id, clickable)
            if self.should_delta_page_be_stored_for_crawling(delta_page):
                self._store_delta_page_for_crawling(delta_page)
        else:
            clickable.clickable_type = ClickableType.SendingAjax
            return clickable


    def handle_delta_pages_has_new_links_clickables_forms(self, clickable, delta_page, parent_page=None,
                                                          xhr_behavior=None):
        delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
        self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
        delta_page.id = self.get_next_page_id()
        self.persistence_manager.update_clickable(parent_page.id, clickable)
        if self.should_delta_page_be_stored_for_crawling(delta_page):
            self._store_delta_page_for_crawling(delta_page)

    def handle_delta_page_has_new_links_ajax_requests__clickables(self, clickable, delta_page, parent_page=None,
                                                                  xhr_behavior=None):
        if xhr_behavior == XHR_Behavior.observe_xhr:
            delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
            delta_page.id = self.get_next_page_id()
            self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
            delta_page.generator_requests.extend(delta_page.ajax_requests)
            delta_page.ajax_requests = []
            self.persistence_manager.update_clickable(parent_page.id, clickable)
            if self.should_delta_page_be_stored_for_crawling(delta_page):
                self._store_delta_page_for_crawling(delta_page)
        else:
            clickable.clickable_type = ClickableType.SendingAjax
            return clickable

    def handle_delta_page_has_new_links_clickables_forms_ajax_requests(self, clickable, delta_page, parent_page=None,
                                                                       xhr_behavior=None):
        if xhr_behavior == XHR_Behavior.observe_xhr:
            delta_page.generator.clickable_type = ClickableType.Creates_new_navigatables
            delta_page.id = self.get_next_page_id()
            self.domain_handler.extract_new_links_for_crawling(delta_page, current_depth=self.current_depth)
            delta_page.generator_requests.extend(delta_page.ajax_requests)
            delta_page.ajax_requests = []
            self.persistence_manager.update_clickable(parent_page.id, clickable)
            if self.should_delta_page_be_stored_for_crawling(delta_page):
                self._store_delta_page_for_crawling(delta_page)
        else:
            clickable.clickable_type = ClickableType.SendingAjax
            return clickable


    def find_form_with_special_params(self, page, login_data):
        login_form = None
        keys = list(login_data.keys())
        data1 = keys[0]
        data2 = keys[1]
        for form in page.forms:
            if form.toString().find(data1) > -1 and form.toString().find(data2) > -1:
                login_form = form
        return login_form


    def convert_action_url_to_absolute(self, form, base):
        form.action = urljoin(base, form.action)
        return form

    def print_to_file(self, item, filename):
        f = open("result/"+ filename, "w")
        f.write(item)
        f.close()

    def should_delta_page_be_stored_for_crawling(self, delta_page):
        for d_pages in self.tmp_delta_page_storage:
            if d_pages.url == delta_page.url:
                page_similarity = calculate_similarity_between_pages(delta_page, d_pages, clickable_weight=1,
                                                                     form_weight=1, link_weight=1)
                if page_similarity >= 0.9:
                    logging.debug("Equal page is already stored...")
                    return False
        for d_pages in self.get_all_crawled_deltapages_to_url(delta_page.url):
            if d_pages.url == delta_page.url:
                page_similarity = calculate_similarity_between_pages(delta_page, d_pages, clickable_weight=1,
                                                                     form_weight=1, link_weight=1)
                if page_similarity >= 0.9:
                    logging.debug("Equal page is already seen...")
                    return False
        return True

    def _store_delta_page_for_crawling(self, delta_page):
        self.tmp_delta_page_storage.append(delta_page)


    def get_all_stored_delta_pages(self):
        return self.tmp_delta_page_storage

    def get_all_crawled_deltapages_to_url(self, url):
        result = self.persistence_manager.get_all_crawled_delta_pages(url)
        return result

    def get_next_page_id(self):
        tmp = self.page_id
        self.page_id += 1
        return tmp


    def extend_ajax_requests_to_webpage(self, web_page, ajax_requests):
        web_page.ajax_requests.extend(ajax_requests)
        self.persistence_manager._extend_ajax_requests_to_webpage(web_page, ajax_requests)

    """
    Is called right before event execution starts. Here you can change the order or delete clickables
    """

    def edit_clickables_for_execution(self, clickables):
        return clickables

    """
    Is called right before an clickable will be executed. You have to return True or False
    """

    def should_execute_clickable(self, clickable):
        # logging.debug(str(clickable.html_class) + " : " + str(clickable.event))
        return True

    def initial_login(self, url_with_loginform, login_data):
        self._page_with_loginform_logged_out = self._get_webpage(url_with_loginform)
        self._login_form = self.find_form_with_special_params(self._page_with_loginform_logged_out, login_data)

        page_with_loginform_logged_in = self._login_and_return_webpage(self._login_form, self._page_with_loginform_logged_out, login_data)

        #if calculate_similarity_between_pages(self._page_with_loginform_logged_out,page_with_loginform_logged_in) < .5:
        #    logging.debug("Initial Login successfull...")
        #    return True

        self.page_with_loginform_logged_in = page_with_loginform_logged_in
        f = open("test1.txt", "w")
        f.write(self._page_with_loginform_logged_out.toString())
        f.close()

        f = open("test2.txt", "w")
        f.write(page_with_loginform_logged_in.toString())
        f.close()
        return calculate_similarity_between_pages(self._page_with_loginform_logged_out, page_with_loginform_logged_in) < 0.5

    def _login_and_return_webpage(self, login_form, page_with_login_form=None, login_data=None):
        if page_with_login_form is None:
            page_with_login_form = self._page_with_loginform_logged_out
        try:
            response_code, html_after_timeouts, new_clickables, forms, links, timemimg_requests = self._form_handler.submit_form(login_form, page_with_login_form, login_data)
        except ValueError:
            return None
        landing_page_logged_in = WebPage(-1, page_with_login_form.url, html_after_timeouts)
        landing_page_logged_in.clickables = new_clickables
        landing_page_logged_in.links = links
        landing_page_logged_in.timeming_requests = timemimg_requests
        landing_page_logged_in.forms = forms

        return landing_page_logged_in

    def handle_possible_logout(self):
        page_with_login_form = self._get_webpage(self._page_with_loginform_logged_out.url)
        login_form = self.find_form_with_special_params(page_with_login_form, self.user.login_data)
        if login_form is not None: #So login_form is visible, we are logged out
            logging.debug("Logout detected, visible login form...")
            page = self._login_and_return_webpage(login_form, self._page_with_loginform_logged_out, self.user.login_data)
            if calculate_similarity_between_pages(page, self._page_with_loginform_logged_out) > 0.5:
                logging.debug("Relogin successfull...continue")
                return True
            else:
                logging.debug("Relogin failed...stop")
                return False

    def _get_webpage(self, url):
        response_code, response_url, html_after_timeouts, new_clickables, forms, links, timemimg_requests = self._dynamic_analyzer.analyze(url, timeout=10)
        result = WebPage(-1, url, html_after_timeouts)
        result.clickables = new_clickables
        result.forms = forms
        result.links = links
        result.timeming_requests = timemimg_requests

        return result




class CrawlState(Enum):
    NormalPage = 0
    EventGeneratedPage = 1
    DeltaPage = 2
    AnalyzrLoginPage = 3
    Login = 4

