"""Scanner rotation keeps revisiting fresh candidates without crowding all others."""
import unittest
from paper_runtime import choose_candidate_batch


class TestWatchlistBatch(unittest.TestCase):
    def test_no_history_preserves_rotation_and_unique_mints(self):
        watch=[str(i) for i in range(10)]
        first,index=choose_candidate_batch(watch,0,{},4,1000)
        second,index2=choose_candidate_batch(watch,index,{},4,1000)
        self.assertEqual(first,["0","1","2","3"])
        self.assertEqual(second,["4","5","6","7"])
        self.assertEqual(index2,8)

    def test_half_of_slots_reserved_for_regular_watchlist_even_with_priority(self):
        watch=["a","b","c","d","e","f","g","h"]
        memories={mint:[(995,1,1,"pool")] for mint in watch[4:]}
        batch,next_index=choose_candidate_batch(watch,0,memories,4,1000)
        self.assertEqual(len(batch),4)
        self.assertEqual(len(set(batch)),4)
        self.assertEqual(batch[:2],["e","f"])
        self.assertEqual(batch[2:],["a","b"])
        self.assertEqual(next_index,2)

    def test_stale_or_malformed_history_is_not_a_priority_signal(self):
        watch=["a","b","c","d"]
        memory={"d":[(700,1,1,"pool")],
                "c":[("bad-ts",1,1,"pool")]}
        batch,index=choose_candidate_batch(watch,0,memory,2,1000)
        self.assertEqual(batch,["a","b"])
        self.assertEqual(index,2)

    def test_recent_candidates_can_be_confirmed_three_times(self):
        watch=["a","b","c","d","e","f","g","h"]
        memory={"g":[(995,1,1,"pool")]}
        seen=[]
        cursor=0
        for now in (1000,1040,1080):
            batch,cursor=choose_candidate_batch(watch,cursor,memory,4,now)
            self.assertIn("g",batch)
            seen.append(batch)
        self.assertEqual(len(seen),3)
        self.assertTrue(any("a" in b for b in seen))

    def test_small_or_empty_watchlist_bounded_without_duplicates(self):
        self.assertEqual(choose_candidate_batch([],0,{},60,1000),([],0))
        self.assertEqual(choose_candidate_batch(["x"],0,{"x":[(999,1,1,"pool")]},60,1000),(["x"],0))


if __name__=="__main__":
    unittest.main()
