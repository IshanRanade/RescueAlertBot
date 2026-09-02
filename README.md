## Requirements

1. The user inputs their info and hits Start, then the bot will start.
2. Once the bot detects and hits Accept, it will send a Telegram text
3. It will continue watching the page for Accept and hit Accept everytime it sees it, except for hospital names in the blocklist
4. The bot will continue doing this indefinitely until the timer runs out, at which point the bot will be killed and stop
5. If the user hits Start again while the bot is running, nothing should happen and the bot should keep running
6. If the user hits Start while the bot is dead, it should start again
7. If the user hits Stop while the bot is running, it should kill the bot, but the user should be able to hit Start again later and restart the bot
8. Only one instance of a bot should ever be running at one time
9. If the user hits Refresh, it should refresh the timer back to the max time
10. If the timer hits 0, it should kill the bot, but the user should still be able to start the bot again later
11. If the bot dies or is stopped, the timer should reset back to 0
12. The timer should automatically reset to the max duration whenever the bot is started
13. Before pressing the accept button, it should make sure it can extract the hospital, patient name, and patient id, and then send that in the telegram message. If any of these steps fail, do not hit Accept, and it should just keep waiting for another notification to show up and tries again.
14. The bot will hard kill itself if it is going 5 min longer than the timer duration in case the timer thread breaks.
15. If there is ever an error sending a telegram message, the bot should kill itself.
16. If the refresh button is pressed, the hard limit of when the bot kills itself if the timer thread dies resets.
17. The sevaro bot UI is accessible from any device on the tailnet
18. All chrome related commands, such as logging in and pressing the accept button, is done with a VPN so that it looks like they requests are coming from the VPN address
19. There is an "Acknowledge Accept" button in the UI.  Once the bot accepts a case, it will keep sending a telegram notification every 30 seconds, and block accepting any new cases until the user hits this "Acknowledge Accept" button.
20. The "Acknowledge Accept" will be grayed out normally, but will become active once the bot has accepted a new case.  Once the user hits this button it will gray out again.
21. If the failsafe goes off, it will kill the bot but the website is still accessible and the user can start the bot again in the future
22. If the bot fails to accept a case that the user is credentialed for, it will notify the user and then continue looking for future accepts

## Blocked hospitals list

The UI has a "Blocked Hospitals" section where you can add/remove hospital
names. If the bot sees a case whose hospital matches any name on the list
(case-insensitive substring match, so "Mercy" blocks every "Mercy ..."
facility), it ignores that case and never accepts it — it just keeps watching
for other cases.

A missing or empty list never causes problems: reads fail open (treated as
"nothing blocked"), and on startup the bot creates the file as `[]` if it
doesn't exist yet, so it's always present for backups.

The list is stored in a JSON file, and its path is set by the
`BLOCKED_HOSPITALS_FILE` env var. On the server it points into the `data`
directory the bot already bind-mounts (for its HTML dumps), so the list lives on
the host and persists across container recreation (`docker compose rm -sf` +
rebuild). The relevant part of the `sevaro-bot` service in `compose.yml`:

```yaml
    volumes:
      - /home/ishan/Data/SevaroBot/data:/app/data
    environment:
      PORT: "3267"
      BLOCKED_HOSPITALS_FILE: "/app/data/blocked_hospitals.json"
      # ...existing TELEGRAM_* vars
```

Because `/app/data` is a directory mount, the file is created there
automatically on startup — no need to pre-create it — and atomic writes work
normally (temp file and target are on the same host filesystem). It's also
included in the nightly backup, since that backs up all of
`/home/ishan/Data/SevaroBot/`.

## To build the package and publish to docker hub:

- SSH onto the home server
- Go to the folder `/home/ishan/Github/RescueAlertBot/ServerBot`

`sudo docker login -u kingish123`

`sudo docker buildx build   --platform linux/amd64   -t kingish123/sevaro-runner:latest   --push .`

## To pull, build, and run on server:

`sudo docker compose rm -sf sevaro-bot`

`sudo docker pull kingish123/sevaro-runner:latest`

`sudo docker compose up --build -d sevaro-bot`

To pull, build, and run all at onced:
`sudo docker compose rm -sf sevaro-bot && sudo docker pull kingish123/sevaro-runner:latest && sudo docker compose up --build -d sevaro-bot`