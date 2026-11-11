# What's new in Shotcast

<!-- Shown in the Windows app after an update (and under "What's new" in its menu), and on the stats
page: each release's heading is the version it ships in, newest first. "Stats page" items show on the
stats page and in the app; "Booth" items only in the app. Plain words, one line each, for the players. -->

## 0.1.118

### Booth
- Saving a change to the stats page's settings (its name, colours, season start or where it goes) republishes the stats page about a minute later, with a notice, instead of waiting for the next round.

## 0.1.117

### Booth
- The stats page's colours can be set in Settings, under Sharing, to match your league's own site; they stay through every publish and update.

## 0.1.116

### Stats page
- Highlight reels play each shot until the ball stops and end soon after, instead of cutting off a chip or approach that finishes inside gimme range.

## 0.1.115

### Booth
- A second round of the day is only called a season best if it beats the first one too.

## 0.1.114

### Stats page
- Tapping an eagle or ace alert plays the hole on video, and a new league record alert plays the shot.

### Booth
- A long practice drive is no longer called "the league's longest": only league rounds set league records.

## 0.1.113

### Stats page
- A "Previous shot" button under round videos and highlight reels goes back to the start of the swing, or the one before.

### Booth
- The hole-result banner ("Par", "Birdie") is no longer mistaken for another player on 4K screens.

## 0.1.112

### Booth
- Faster read of the other players' scores on 4K screens: about twice as quick, and it catches a few scores it used to miss.

## 0.1.111

### Stats page
- Videos start right before the swing: Watch buttons, highlight reels and record clips no longer show several seconds of the ball sitting there first.
- Double-tap the right side of a video to skip ahead 5 seconds, the left to go back (tap again for more).
- A "Next shot" button on round videos and highlight reels jumps straight to the next swing.

## 0.1.110

### Stats page
- Alternate shot approach distances are right: a shot was being kept as finishing at gimme range (or in the hole) instead of where the partner played the next one from, so highlights said "stuffs it to 4 ft" for a 23-footer. Past rounds are fixed too.

## 0.1.109

### Stats page
- Back works after switching tabs: going from Home to the schedule (or any tab, or a course's details) and pressing Back returns to where you were.

## 0.1.108

### Booth
- Faster read of each shot's setup: the slope readout no longer takes most of the time, so the booth has the lie and the player sooner.

## 0.1.107

### Booth
- More varied calls while the ball's in the air: the booth sees its last few and stops reaching for the same words ("peeling", "bending… in play").
- A shot that can't reach the green (a drive, a long second, a layup) is called at the fairway or the layup, not the flag; reach is judged from the player's own usual drive.
- Truer left and right calls in flight: the projection is now tuned to where a thousand of the season's shots actually finished, and it follows your aim, so aiming right of the flag to draw it back is called at the flag, not as a pull left.
- Laying up or chipping out sideways from trouble: when you click a spot well short of the flag, or a tree blocks the line, the booth calls the shot at that spot, not "at the flag".
- Draws and fades are called against the flag: a ball aimed right that's drawing back is called coming back to the pin, not as a hook, and "short" or "long" counts the roll, with no yardages guessed mid-flight.
- Layups, chip-outs and draws or fades working back to the flag now get their own line written before the swing, so they're called the instant the ball's struck instead of a beat later.
- A long shot from the fairway or rough is called (and recorded) as a 3-wood, not a driver: newer GSPro shows the driver for any long shot, but a driver only comes out on the tee. Those already in your stats are corrected too.
- Chips and pitches are called on how they finished, not on their start line, curve or strike numbers.
- Reach counts the lie: from deep rough or sand the booth knows the green may be out of reach, and afterwards it can tell a good recovery from a shot that should have found the green.

## 0.1.106

### Stats page
- Tapping ▶ Highlights now plays the round's highlight reel; before, it did nothing.

## 0.1.105

### Booth
- The booth only reads GSPro: anything in front of it (a browser, Discord, a chat window) or beside a windowed GSPro is blacked out before it's read, so it never ends up in the booth's log or a problem report.

## 0.1.103

### Stats page
- Alternate-shot hole replays draw each stroke's line to where the partner played the next one.

### Booth
- The booth's own badge, health list and popups are kept out of its screen reads (its warning was being read as the course name). They also no longer show in a stream that captures the screen.
- In a scramble, the ball the team played on keeps the lie from GSPro's log; the pick screen's read is used for the other balls.

## 0.1.102

### Booth
- Swapping players' order on GSPro's setup screen no longer mixes up whose shot was whose (a holed putt was being given to the partner's stroke before it).
- A scramble or alternate shot with more than two players takes its teams from the team colours on GSPro's PLAYERS tab.

## 0.1.101

### Booth
- When the booth isn't sure of a closest-to-the-pin tee shot (say a gimme that might be on the fringe), a box on the sim asks before it goes on Squabbit. Fix the distance if it's off and post it, or leave it to enter yourself.
- GSPro's alternate shot mode is called as one team ball with one score, not two players' scores.
- Wentworth by the Sea gets its own card (pars and yardages) even when it's installed under another course's name.

## 0.1.100

### Stats page
- The league home shows the weeks due on the same date together, with arrows to look back at past dates.

### Booth
- On a league round, a box on the sim asks whether to update Squabbit as you play. Say yes and each hole's score goes on your team's week card as you finish it, along with closest-to-the-pin tee shots. Nothing goes over anything already on your card.

## 0.1.99

### Stats page
- Closest-to-the-pin distances show in feet, however far.

### Booth
- The booth reads the scramble pick screen on older GSPro versions too (USE buttons), and reads each ball's lie more reliably.
- Closest-to-the-pin distances are called in feet, however far.

## 0.1.98

### Booth
- Commentary lines that run a few words long now air whole instead of being cut short or skipped.
- On the driving range, the booth really does stop re-reading the setup screen (the sky behind it looked like a lit tab).

## 0.1.97

### Stats page
- League rounds show their **net score** (after handicap strokes) on the Rounds tab and the round's page.

### Booth
- Between rounds on the range, the booth no longer re-reads the setup screen several times a second.

## 0.1.96

### Stats page
- The league card shows each week's **closest to the pin** on every par 3 played on the sim.
- Scramble rounds record every ball's distance to the pin and lie, including the balls the team didn't pick.
- A conceded putt's distance is the ball's real distance to the hole, not the gimme circle.

### Booth
- The ball's resting lie is named far more often: the booth watches the ball stop much more closely.
- A ball pausing on a crest before rolling on is read again where it finally stops.
- A scramble's ball pick is no longer counted as a mulligan.
- Alternate shot rounds use GSPro's own scores.
- A league round started with the wrong number of holes and quit after the right ones counts for the week.

## 0.1.95

### Booth
- A locked PC or a sleeping display no longer fills the log with hundreds of errors a minute.

## 0.1.94

### Stats page
- A **hole replay** shows the card's score, marked "(max)", when GSPro capped the hole at its maximum.
- Hole replays use the exact pin GSPro 4.0 logs for the hole when it has one.

### Booth
- Reading the screen is several times faster, so calls come quicker after a shot.
- A ball into a **bush** is called a bush when GSPro says it was one, not a tree.
- GSPro 4.0's **Semi**, **Pine** and **Timber** lies are read from the screen.
- The scorecard check is lighter between holes in GSPro 4.0.
- The resting lie is right when the next player is up the moment the ball stops.
- Minimaps sent to Jev are big enough for it again.

## 0.1.93

### Stats page
- The home page's handicap shows when it's from **no-mulligan rounds** only.

## 0.1.92

### Booth
- **Phone alerts** work without the live chat: the site's table is under Settings → Sharing → **Site table (chat and alerts)**, next to the **Phone alerts** switch.

## 0.1.91

### Stats page
- **Alerts** works in Firefox too.
- "We're live" alerts can be for any round or for league and tournament rounds only.
- Alerts only offers what your booth sends: no league alerts without a league, no "We're live" without the live stream.
- The site password is optional.

### Booth
- The booth knows when a chatter is a league player, or one of the players out there typing between shots.
- The booth doesn't make up predictions it never made.

## 0.1.90

### Booth
- A **re-tee after water** in GSPro 4.0 counts the penalty stroke (GSPro leaves it out), and the drive isn't counted as a fairway hit.
- GSPro 4.0's splash is read from its log again, so water is called the moment the ball goes in.

## 0.1.88

### Booth
- Works with **GSPro 4.0**: the Settings folder check and Find GSPro know its new folder.
- **Out of bounds** gets called on the shot itself in GSPro 4.0, not only before the next one.
- The lie comes from the player box in GSPro 4.0 and the beta ("Deep" is deep rough), with the minimap as backup.
- A round quit part-way through keeps its last finished hole.
- A drive hit right after **Tee Off** from a course's range is called on the right hole.
- Each round stores GSPro's green and fairway firmness (4.0) and the launch monitor.

## 0.1.87

### Stats page
- **Phone alerts:** tap **🔔 Alerts** at the top of the stats page and pick what you'd like a
  notification for: the booth going live, a league round posted, a league record falling, an eagle or
  an ace, or the week's recap. On an iPhone, tap **📲 Get the app** first to add the page to your
  home screen.
- **What's new:** a card like this one on the stats page when something new arrives.

### Booth
- A **What's new** window after each update, and in the tray menu any time.
- **Phone alerts** are on whenever the live chat is (Settings → Chat → Phone alerts).

## 0.1.86

### Booth
- A player who only appeared during setup no longer shows up in the round.
- Updates can install with GSPro still open, once nobody has played for a while.

## 0.1.85

### Booth
- A downloaded update installs itself as Shotcast starts, or once GSPro has been closed for 30 minutes
  (tray menu: Install updates automatically).

## 0.1.84

### Stats page
- **League recap:** each league week gets a recap video, told like a tournament broadcast, with the
  results card, a net leaderboard and the shots that changed the lead (Watch → Round replays).
- **Highlight reels:** every round gets a reel of its best moments, a few minutes long (▶ Highlights on
  the round).

## 0.1.83

### Stats page
- **League board:** the Home tab shows the league week's board and the season standings, kept up to
  date from Squabbit.
- The Live tab shows the week's board under the video during a league round.

## 0.1.82

### Stats page
- The live chat knows which hole and shot you were watching, and the booth catches you up if your
  stream fell behind.
- Chat can't be sent from well behind live: jump to live first.

### Booth
- The tray asks before Quit, so the stream doesn't go off air by mistake.

## 0.1.81

### Stats page
- The booth reads the live chat when a shot leaves it a gap to talk about it.

### Booth
- Fewer wrong calls: no false mulligan after a hole-out, and holed putts name the right score.
- The tray shows the booth starting until it's ready, with a tick box for the "booth is ready" notice.

## 0.1.80

### Stats page
- Each round gets its own live chat.
- A round's buttons are grouped into Watch and Share.

### Booth
- **Practice mode:** the booth stays quiet and saves nothing while you practise.
- Exclude or delete a round from the Rounds tab on the PC's own stats page.
