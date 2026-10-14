"""Discovery tests for official GeckoTerminal trending pools as a separate source."""
import unittest

from market_discovery import discover_mints

MINT_A = "DezXAZ8z7PnrnRJjz3wXBoRgixCaKGiyVFs2RmmJ6Bq"
MINT_B = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"


def pools(mint):
    return {"data":[{"relationships":{"base_token":{"data":{"id":"solana_"+mint}}}}]}


class TestTrendingDiscovery(unittest.TestCase):
    def test_trending_is_scanned_ahead_of_promotional_feeds(self):
        called=[]
        def fetch(url,timeout):
            called.append(url)
            if "trending_pools" in url:return pools(MINT_B)
            if "new_pools" in url:return pools(MINT_A)
            return [{"chainId":"solana","tokenAddress":MINT_A}]
        values,stats=discover_mints(fetch=fetch,gecko_pages=1,max_mints=10)
        self.assertEqual(values,[MINT_B,MINT_A])
        self.assertEqual(stats["source_counts"]["gecko_trending_pools"],1)
        self.assertEqual(stats["total_unique"],2)
        self.assertEqual(len(called),7)
        self.assertFalse(stats["partial_failure"])
        self.assertIn("Discovery only",stats["disclaimer"])

    def test_trending_timeout_fails_visibly_without_suppressing_new_pools(self):
        def fetch(url,timeout):
            if "trending_pools" in url:raise TimeoutError("public quota exceeded")
            if "new_pools" in url:return pools(MINT_B)
            return [{"chainId":"solana","tokenAddress":MINT_A}]
        values,stats=discover_mints(fetch=fetch,gecko_pages=1,max_mints=10)
        self.assertEqual(set(values),{MINT_A,MINT_B})
        self.assertTrue(stats["partial_failure"])
        errors=stats["errors"]
        self.assertTrue(any(x["source"]=="gecko_trending_pools" and "quota" in x["error"] for x in errors))

    def test_explicit_no_gecko_mode_keeps_five_dex_feeds(self):
        calls=[]
        def fetch(url,timeout):
            calls.append(url)
            return [{"chainId":"solana","tokenAddress":MINT_A}]
        tokens,stats=discover_mints(fetch=fetch,gecko_pages=0)
        self.assertEqual(tokens,[MINT_A])
        self.assertEqual(len(calls),5)
        self.assertNotIn("gecko_trending_pools",stats["source_counts"])


if __name__=="__main__":
    unittest.main()
