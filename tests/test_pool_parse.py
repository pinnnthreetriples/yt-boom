import unittest

from modules.pool_manager import parse_video_text

V = "https://www.youtube.com/watch?v="


class TestPoolParse(unittest.TestCase):
    def test_videos_separated_by_commas_on_one_line(self):
        text = f"{V}hJrAgu0r9o8,{V}jy-1yUMwnfw,{V}lnH0PePZX5c,{V}evZgKS6tJfs"
        urls, invalid = parse_video_text(text)
        self.assertEqual(urls, [V + "hJrAgu0r9o8", V + "jy-1yUMwnfw", V + "lnH0PePZX5c", V + "evZgKS6tJfs"])
        self.assertEqual(invalid, 0)

    def test_mixed_separators_duplicates_and_comments(self):
        text = f"{V}aaaaaaaaaaa, {V}bbbbbbbbbbb ; {V}ccccccccccc\n{V}ddddddddddd {V}eeeeeeeeeee # note\n{V}aaaaaaaaaaa"
        urls, invalid = parse_video_text(text)
        self.assertEqual([u[-11:] for u in urls], ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd", "eeeeeeeeeee"])
        self.assertEqual(invalid, 0)

    def test_short_links_and_garbage_counted(self):
        urls, invalid = parse_video_text("https://youtu.be/aaaaaaaaaaa, not a video, https://youtu.be/bbbbbbbbbbb")
        self.assertEqual(len(urls), 2)
        self.assertEqual(invalid, 1)

if __name__ == "__main__":
    unittest.main()
