# Pending tasks

## Instructions

When starting these tasks, first branch off a new branch. These can be long and intricate changes that might
end up not being wanted. Each session should have its own branch so as to not pollute the main branch which
is a known "good" state.  

Each item should be at minimum one commit. Never mix tasks in a single commit. Tasks can span multiple commits
if the work is large enough.  

Before starting any work, investigate each task and clear up any questions ahead of time. The idea is to only
need user interaction at the beginning of a session and at the end of the session. The session should be
as unattended as possible.

As tasks are done, prefix the existing entry here with a [done]. If a task is unable to be completed and is
blocked for any reason, leave it and present the reasons why it was not done at the end of the session.  

When the tasks are addressed, make sure the branch is committed and up to date, but not pushed, and a local
staging environment should be setup. Present the changes done in the session and the URL for the staging
deployment.  

## Task list

Redesign session (branch `redesign`, plan in docs/plans/redesign.md):

- Touch fixes: pick on a completed tap, not pointerdown; sheets ignore the finger that opened them.
- Data layer: favourites, settings (dose presets), one-off foods, missing-carbs by item, quick-carb estimates, explicit saved-meal updates, meal-moment bolus reference + meal insight.
- New visual design system (tokens, light/dark following the system).
- Phone rewrite: status home, bottom action bar, tap-first insulin/food/note flows, entry detail with the meal's reference and response.
- Desktop restructure: sectioned console (Overview, Logbook, Meals, Library, Phone), favourites + usage in the library, missing-carbs to-do, reference on existing meals, phone setup panel.
- Local staging instance on this machine from a live-DB snapshot.
