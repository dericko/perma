var linkyUrl = '';
var rawUrl = '';
$(document).ready(function() {console.log(web_base);
  $('#linky-confirm').modal({show: false});
  $('#rawUrl').focus();
  
  $('#linker').submit(function() {
		linkIt();
		return false;
	});

});

function linkIt(){
  $('#linkyUrl').toggleClass('text-center');
  $('#linky-preview img').attr('src', 'http://placekitten.com/500/400');
  $('#linkyUrl a').attr('href', '').html('<i class="icon-spinner icon-spin icon-2x text-center"></i>');
  rawUrl = $("#rawUrl").val();
  var request = $.ajax({
    url: "http://hlsl7.law.harvard.edu:5002/api/linky/",
    type: "POST",
    data: {url: rawUrl},
    dataType: "json"
  });
  request.done(function(data) {
    linkyUrl = web_base + data.linky_url;
    $('#linkyUrl').toggleClass('text-center');
    $('#linkyUrl a').html(linkyUrl).attr('href', linkyUrl);
    $('#linky-preview img').attr('src', linkyUrl);
  });
  request.fail(function(jqXHR, responseText) {
    console.log( jqXHR );
  });
  
  $('#linky-confirm').modal('show');
  $('#saveLinky').on('click', function(event){
    event.preventDefault();
    $('#linky-confirm').modal('hide');
  });
}

$('#linky-confirm').on('hidden', function () {
  $('#rawUrl').val('').focus();
  $('#linky-list').fadeIn();
  $('#linky-list tbody').append('<tr><td><a href="' + linkyUrl + '">' + linkyUrl + '</a></td><td><a href="' + rawUrl + '">' + rawUrl + '</a></td></tr>');
});