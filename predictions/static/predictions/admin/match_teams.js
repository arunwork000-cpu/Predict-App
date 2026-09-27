// Match admin: show only the chosen sport's teams in Team A / Team B, and only
// those two teams in Winner. The sport -> teams map is embedded on the Sport
// <select> as data-teams (see MatchAdminForm in predictions/admin.py).
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    var sportSelect = document.getElementById("id_sport");
    var teamA = document.getElementById("id_team_a");
    var teamB = document.getElementById("id_team_b");
    var winner = document.getElementById("id_winner");
    if (!sportSelect || !teamA || !teamB || !winner) {
      return;
    }

    var teamsBySport = {};
    try {
      teamsBySport = JSON.parse(sportSelect.dataset.teams || "{}");
    } catch (e) {
      return;
    }

    function setOptions(select, items, placeholder) {
      var previous = select.value;
      select.innerHTML = "";
      var blank = document.createElement("option");
      blank.value = "";
      blank.textContent = placeholder;
      select.appendChild(blank);
      items.forEach(function (item) {
        var option = document.createElement("option");
        option.value = String(item[0]);
        option.textContent = item[1];
        if (option.value === previous) {
          option.selected = true;
        }
        select.appendChild(option);
      });
    }

    function refreshWinner() {
      var picked = [];
      [teamA, teamB].forEach(function (select) {
        var option = select.options[select.selectedIndex];
        if (select.value && option) {
          picked.push([select.value, option.textContent]);
        }
      });
      setOptions(winner, picked, "---------");
    }

    function refreshTeams() {
      var teams = teamsBySport[sportSelect.value] || [];
      var placeholder = sportSelect.value ? "---------" : "Select a sport first";
      setOptions(teamA, teams, placeholder);
      setOptions(teamB, teams, placeholder);
      refreshWinner();
    }

    sportSelect.addEventListener("change", refreshTeams);
    teamA.addEventListener("change", refreshWinner);
    teamB.addEventListener("change", refreshWinner);
  });
})();
