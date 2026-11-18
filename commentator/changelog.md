# What's new in Shotcast

<!-- Shown in the Windows app after an update (and under "What's new" in its menu), and on the stats
page: each release's heading is the version it ships in, newest first. "Stats page" items show on the
stats page and in the app; "Booth" items only in the app. Plain words, one line each, for the players. -->

## 0.1.150

### Stats page
- A player who joined late or left partway now counts as a part-played round, not a finished one, so their score isn't compared with full rounds.
- Shots the booth caught without a name (no round started in it) are named from GSPro's own record of the round: who hit them, which hole, and which stroke.

### Booth
- Players added or removed in GSPro partway through a round are followed: someone who leaves keeps the holes they played and nothing waits on them, and someone who joins gets only their own holes. GSPro opening its player menu mid-round is the cue to check, so a removal is picked up after the next hole.
- A phone alert when another program has covered GSPro for over 3 minutes, which leaves the booth unable to read the screen.

## 0.1.149

### Booth
- Fixed the booth going blind after names were typed with the Windows touch keyboard: it no longer mistakes the keyboard's invisible handwriting layer for a window covering GSPro (0.1.148's fix missed it).

## 0.1.148

### Booth
- Fixed the booth going blind after names were typed with the Windows touch keyboard: it no longer mistakes the keyboard's invisible handwriting layer for a window covering GSPro.

## 0.1.147

### Booth
- The round's opener, wrap-up, news asides and chat takes are written by a newer, quicker model that sticks to the facts better.
- Players and guests can go by a nickname on air: introduced as First "Nickname" Last, then called by the nickname.
- Fixed GSPro being misread when it draws black bars around its picture, which could turn a league round into practice.

## 0.1.146

### Booth
- Turned Squabbit down? The booth now asks whether it's a league round, and "No, practice" keeps it out of the league.

### Stats page
- Phone alerts: turning alerts on or changing them no longer leaves extra copies of your phone signed up.

## 0.1.145

### Stats page
- A scramble round's "Shots used" now adds up to the score when a hole was picked up: the last stroke counts for whoever hit it.

## 0.1.144

### Booth
- The commentators no longer hand their own predictions to each other ("You called for simple" from the one who called it).
- A solo round no longer says a player has never played a course they've played as part of a team.

## 0.1.143

### Stats page
- Courses under the League filter now include the league's scramble and alternate-shot cards, one per team; practice stays individual rounds only.

### Booth
- League openers lead with the league race: the week's scores to beat, the standings, a rival team.
- How the league has scored a hole now comes up on the holes that stand out (playing tough, the hardest or easiest of the round, or nobody has birdied it), up to three holes a round.

## 0.1.142

### Stats page
- The hole replay shows a scramble's out-of-bounds tee shots, where they landed, marked out of bounds.
- A round's opening call now shows with the round even when the booth had sat idle before it.

### Booth
- Scramble closest to the pin: once both balls are in, the booth asks about entering the team's closer ball on the green, with its distance in feet and inches.
- In a scramble, a player no longer gets credited with his partner's distance to the pin.
- Fewer wrong running scores, no "longest drive in the league" when a longer one came earlier that day, and penalty calls no longer say "unmentioned".

## 0.1.141

### Booth
- Super-ultrawide (32:9) screens now read the hole's par and length and the club box, and the booth spots a lefty's flipped HUD on screens wider than 16:9.
- The screen check now recognises GSPro's setup tabs on 32:9 screens.

## 0.1.140

### Booth
- The booth only warns about a GSPro screen size it hasn't played a round at, so turning a projector on and off, or a windowed GSPro, no longer raises the warning again and again.

## 0.1.139

### Booth
- Launch monitors that don't send a carry (like springbok's MLM2PRO-GSPro-Connector) now get the in-flight call on normal full swings, from a carry worked out from the ball numbers. Tops, thin shots and big hooks or slices are left without one.

## 0.1.138

### Booth
- Club speed, path, face and attack angle for more launch monitors, read from their own software as you play: FlightScope (Mevo, Mevo+, X3), rela and OpenSkyPlus (SkyTrak), and springbok's MLM2PRO-GSPro-Connector (MLM2PRO, Garmin R10 and the monitors it reads off the screen).
- On GSPro 3.59, full swings from FlightScope and rela are called while the ball is still in the air, like Uneekor's.
- Problem reports include the launch monitor's own files, with sign-ins and keys blanked out, so we can learn monitors we haven't seen yet.

## 0.1.137

### Stats page
- A player's page lists their rounds 20 at a time, with a button for more.

## 0.1.136

### Stats page
- The stats page opens faster: a round's scorecard and details load when you open the round, so the page stays light however many rounds you've played.
- The Rounds list shows 50 rounds at a time, with a button for more.

## 0.1.135

### Stats page
- The stats page builds much faster and stays quick as the seasons pile up, even after years of play.
- Gapping is worked out from each player's swings over the last 12 months, so it shows the bag as it is now.
- A round and its shots can be excluded, deleted or edited from three minutes after the round ends, not while it's being played. Practice shots can still be edited straight away.

### Booth
- The booth starts right away, even after an update: the stats page builds in the background.
- Before an update changes the season file, Shotcast keeps a copy of it, so you can go back to the earlier version with your stats as they were. If the copy can't be made, the booth doesn't start and says why.
- The booth avoids repeating itself based on the last year of commentary, which keeps its start quick after years of play.

## 0.1.134

### Stats page
- The side view of a shot draws the ground sloping from where it was hit to where it landed, so downhill and uphill shots read clearly, and says how far below or above the tee it came down.

## 0.1.133

### Stats page
- Tap a shot in a hole replay to watch it from the side and overhead: the ball's height all the way out, its apex, carry, total, hang time and how steeply it came down, with the landing in slow motion up close (GSPro 3.59 and later).

## 0.1.132

### Stats page
- Each player's page has a dispersion chart: where each club's full swings finish, with a ring around 95% of them, and the gapping table shows each club's typical miss and how wide it sprays.

## 0.1.131

### Stats page
- Hole replays draw each shot the way GSPro tracked it, its real flight and roll, with a ○ where it landed (GSPro 3.59 and later).
- Carry, total, lies and where each shot finished come straight from GSPro's own record of the round, so they're exact, and tee shots and drops start from the right spot.

## 0.1.130

### Booth
- In practice rounds the booth knows who's leading and by how much, so it no longer calls the leader "one behind" or invents a lead.
- A call the booth makes ("I'm calling it in") is judged only by the shot it was about, never held to a later one or claimed as coming true when it didn't.
- Every putt is called for what it's actually for: a par putt is no longer a "birdie putt".

## 0.1.129

### Booth
- Turning the booth off only quiets the commentary: the live stream keeps going (with the game's sound), so the round still gets its replay and highlight reel.
- A league-night round set up for all 18 holes now gets the Squabbit question on the tee of the week's first hole, and posts only the week's holes.

## 0.1.128

### Booth
- Much lighter on the PC between rounds: on the driving range the booth no longer looks for the setup screen and checks the screen a third as often, and its text reading no longer keeps CPU cores busy while idle, so GSPro stays smooth.

## 0.1.126

### Booth
- Ultrawide screens (21:9 and wider) are read in full: the top data tile, the setup screen's tabs, Play button and player names (five cards to a row) all line up.

## 0.1.125

### Booth
- The booth can use a second voice service (Microsoft Azure) once Google's free monthly allowance runs low, picking the voices at the start of each round so they never change mid-round.

## 0.1.124

### Booth
- GSPro in a window (any size) is read as well as full screen: its title bar no longer stops the booth seeing who's up or starting the round.
- A round whose start the booth missed is picked up once a scorecard shows it.

## 0.1.123

### Stats page
- Club gapping uses only shots from the tee and fairway: from the rough GSPro often records a shorter club than the one swung. A club with too few of those is starred.

## 0.1.122

### Booth
- Chipping off the green with a wedge gets its own (fun) call instead of being called a putt, and a mention in the sign-off.
- Calls written before the swing no longer guess the club ("the iron holds its line" over a driver).
- Quitting on a new hole's tee no longer introduces that hole before the sign-off.
- The sign-off no longer opens with "Good afternoon".

## 0.1.121

### Stats page
- Scramble tee shots saved as finishing on the tee get their real lie (fairway, rough, sand) in past rounds.

### Booth
- A shot is never saved as coming to rest on the tee.
- After a hole, the booth gives the right overall score, not just the hole's ("two under overall" after an eagle at three under).
- In a scramble, the booth no longer says which ball the team picked before everyone has hit, or sets up a player's next shot before their partner has played.

## 0.1.120

### Booth
- Shots are followed to where they stop again, instead of being called from mid-flight ("found the fairway", "missed the green") when the camera passed over sky or sand.

## 0.1.119

### Stats page
- Mid-hole shots that were saved as a tee shot (and the penalty strokes that came with them) are corrected in past rounds, and are no longer misread.
- A scramble's hole replay shows the team's stroke into a penalty area.

### Booth
- When a player re-tees right after a partner's swing, the call is about the right player.
- Scramble scores are spoken as the team's, and one partner's wet ball isn't called the team's penalty.
- A replayed putt or shot is called from the right distance.

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
