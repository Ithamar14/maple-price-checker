"""The crawler, walking to an NPC and selling after each room. See walker.run_full."""
import crawler
import walker

if __name__ == "__main__":
    crawler.main(walker.run_full)
