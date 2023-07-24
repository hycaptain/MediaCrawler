import os
import random
import asyncio
from asyncio import Task
from typing import Optional, List, Dict, Tuple
from argparse import Namespace

from playwright.async_api import Page
from playwright.async_api import BrowserContext
from playwright.async_api import async_playwright
from playwright.async_api import BrowserType

import config
from tools import utils
from .exception import *
from .login import XHSLogin
from .client import XHSClient
from models import xiaohongshu as xhs_model
from base.base_crawler import AbstractCrawler
from base.proxy_account_pool import AccountPool


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    browser_context: BrowserContext
    xhs_client: XHSClient
    account_pool: AccountPool

    def __init__(self):
        self.index_url = "https://www.xiaohongshu.com"
        self.command_args: Optional[Namespace] = None # type: ignore
        self.user_agent = utils.get_user_agent()

    def init_config(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    async def start(self):
        account_phone, playwright_proxy, httpx_proxy = self.create_proxy_info()
        async with async_playwright() as playwright:
            # Launch a browser context.
            chromium = playwright.chromium
            self.browser_context = await self.launch_browser(
                chromium,
                playwright_proxy,
                self.user_agent,
                headless=config.HEADLESS
            )
            # stealth.min.js is a js script to prevent the website from detecting the crawler.
            await self.browser_context.add_init_script(path="libs/stealth.min.js")
            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)

            # Create a client to interact with the xiaohongshu website.
            self.xhs_client = await self.create_xhs_client(httpx_proxy)
            if not await self.xhs_client.ping():
                login_obj = XHSLogin(
                    login_type=self.command_args.lt,
                    login_phone=account_phone,
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES
                )
                await login_obj.begin()
                await self.xhs_client.update_cookies(browser_context=self.browser_context)

            # Search for notes and retrieve their comment information.
            await self.search_posts()

            utils.logger.info("Xhs Crawler finished ...")

    async def search_posts(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("Begin search xiaohongshu keywords")

        for keyword in config.KEYWORDS.split(","):
            utils.logger.info(f"Current keyword: {keyword}")
            max_note_len = config.MAX_PAGE_NUM
            page = 1
            while max_note_len > 0:
                note_id_list: List[str] = []
                posts_res = await self.xhs_client.get_note_by_keyword(
                    keyword=keyword,
                    page=page,
                )
                _semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                task_list = [
                    self.get_note_detail(post_item.get("id"), _semaphore)
                    for post_item in posts_res.get("items", {})
                ]
                note_details = await asyncio.gather(*task_list)
                for note_detail in note_details:
                    if note_detail is not None:
                        await xhs_model.update_xhs_note(note_detail)
                        note_id_list.append(note_detail.get("note_id"))
                page += 1
                max_note_len -= 20
                utils.logger.info(f"Note details: {note_details}")
                await self.batch_get_note_comments(note_id_list)

    async def get_note_detail(self, note_id: str, semaphore: "asyncio.Semaphore") -> Optional[Dict]:
        """Get note detail"""
        async with semaphore:
            try:
                return await self.xhs_client.get_note_by_id(note_id)
            except DataFetchError as ex:
                utils.logger.error(f"Get note detail error: {ex}")
                return None

    async def batch_get_note_comments(self, note_list: List[str]):
        """Batch get note comments"""
        utils.logger.info(f"Begin batch get note comments, note list: {note_list}")
        _semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for note_id in note_list:
            task = asyncio.create_task(self.get_comments(note_id, _semaphore), name=note_id)
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, note_id: str, semaphore: "asyncio.Semaphore"):
        """Get note comments"""
        async with semaphore:
            utils.logger.info(f"Begin get note id comments {note_id}")
            all_comments = await self.xhs_client.get_note_all_comments(note_id=note_id, crawl_interval=random.random())
            for comment in all_comments:
                await xhs_model.update_xhs_note_comment(note_id=note_id, comment_item=comment)

    def create_proxy_info(self) -> Tuple[Optional[str], Optional[Dict], Optional[str]]:
        """Create proxy info for playwright and httpx"""
        if not config.ENABLE_IP_PROXY:
            return None, None, None
        utils.logger.info("Begin proxy info for playwright and httpx ...")
        # phone: 13012345671  ip_proxy: 111.122.xx.xx1:8888
        phone, ip_proxy = self.account_pool.get_account()
        playwright_proxy = {
            "server": f"{config.IP_PROXY_PROTOCOL}{ip_proxy}",
            "username": config.IP_PROXY_USER,
            "password": config.IP_PROXY_PASSWORD,
        }
        httpx_proxy = f"{config.IP_PROXY_PROTOCOL}{config.IP_PROXY_USER}:{config.IP_PROXY_PASSWORD}@{ip_proxy}"
        return phone, playwright_proxy, httpx_proxy

    async def create_xhs_client(self, httpx_proxy: str) -> XHSClient:
        """Create xhs client"""
        utils.logger.info("Begin create xiaohongshu API client ...")
        cookie_str, cookie_dict = utils.convert_cookies(await self.browser_context.cookies())
        xhs_client_obj = XHSClient(
            proxies=httpx_proxy,
            headers={
                "User-Agent": self.user_agent,
                "Cookie": cookie_str,
                "Origin": "https://www.xiaohongshu.com",
                "Referer": "https://www.xiaohongshu.com",
                "Content-Type": "application/json;charset=UTF-8"
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
        )
        return xhs_client_obj

    async def launch_browser(
            self,
            chromium: BrowserType,
            playwright_proxy: Optional[Dict],
            user_agent: Optional[str],
            headless: bool = True
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("Begin create browser context ...")
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % self.command_args.platform) # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy, # type: ignore
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy) # type: ignore
            browser_context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent
            )
            return browser_context

    async def close(self):
        """Close browser context"""
        await self.browser_context.close()
        utils.logger.info("Browser context closed ...")