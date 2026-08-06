import displayio
from adafruit_display_shapes.rect import Rect
from adafruit_display_text.label import Label
from adafruit_matrixportal.matrix import Matrix

from config import config, dim_color


class TrainBoard:
	"""
		get_new_data is a function that is expected to return an array of dictionaries like this:

		[
			{
				'line_color': 0xFFFFFF,
				'destination': 'Dest Str',
				'arrival': '5'
			}
		]
	"""
	def __init__(self, get_new_data):
		self.get_new_data = get_new_data
		
		self.matrix = Matrix(bit_depth=4)
		self.display = self.matrix.display

		self.parent_group = displayio.Group()

		self.heading_label = Label(config['font'], anchor_point=(0,0))
		self.heading_label.x = 0
		self.heading_label.y = 3  # Shift down by 3 pixels
		self.heading_label.color = config['heading_color']
		self.heading_label.text=config['heading_text']
		self.parent_group.append(self.heading_label)

		self.trains = []
		for i in range(config['num_trains']):
			self.trains.append(Train(self.parent_group, i))


		self.display.root_group = self.parent_group

	def update_blink(self):
		"""Update blink state for all skip trains"""
		import time
		# Fast blink for skip trains using system time (no additional memory)
		blink_on = (int(time.monotonic() * 2) % 2) == 0  # Toggle every 0.5 seconds
		for train in self.trains:
			if train.is_skip_mode:
				train.blink_state = blink_on
				if train.skip_stripes:
					for stripe in train.skip_stripes:
						stripe.hidden = not blink_on

	def refresh(self) -> bool:
		print('Refreshing train information...')
		
		train_data = self.get_new_data()
		
		if train_data is not None:
			print('Reply received.')
			for i in range(config['num_trains']):
				if i < len(train_data):
					train = train_data[i]
					self._update_train(i, train)
				else:
					self._hide_train(i)
			
			print('Successfully updated.')
		else:
			print('No data received. Clearing display.')

			for i in range(config['num_trains']):
				self._hide_train(i)

	def _hide_train(self, index: int):
		self.trains[index].hide()

	def _update_train(self, index: int, train_info: dict):
		self.trains[index].update(train_info)

class Train:
	def __init__(self, parent_group, index):
		# Add 1 extra pixel for entries 2 and 3 to create visual separation
		extra_spacing = 1 if index >= 1 else 0
		self.y = (int)(config['character_height'] + config['text_padding']) * (index + 1) + extra_spacing

		# Create two rectangles for the split-color bar
		top_rect_height = config['train_line_height'] // 2
		bottom_rect_height = config['train_line_height'] - top_rect_height
		self.line_rect_top = Rect(0, self.y, config['train_line_width'], top_rect_height, fill=config['loading_line_color'])
		self.line_rect_bottom = Rect(0, self.y + top_rect_height, config['train_line_width'], bottom_rect_height, fill=config['loading_line_color'])

		# Skip stripe rectangles (created only when needed)
		self.skip_stripes = None
		self.is_skip_mode = False
		self.blink_state = False

		self.destination_label = Label(config['font'], anchor_point=(0,0))
		self.destination_label.x =  config['train_line_width'] + 2
		self.destination_label.y = self.y + 3  # Shift text down by 3 pixels
		self.destination_label.color = config['text_color']
		self.destination_label.text = config['loading_destination_text'][:config['destination_max_characters']]

		self.min_label = Label(config['font'], anchor_point=(0,0))
		self.min_label.x = config['matrix_width'] - (config['min_label_characters'] * config['character_width']) + 1
		self.min_label.y = self.y + 3  # Shift text down by 3 pixels
		self.min_label.color = config['text_color']
		self.min_label.text = config['loading_min_text']

		self.group = displayio.Group()
		self.group.append(self.line_rect_top)
		self.group.append(self.line_rect_bottom)
		self.group.append(self.destination_label)
		self.group.append(self.min_label)

		parent_group.append(self.group)

	def show(self):
		self.group.hidden = False

	def hide(self):
		self.group.hidden = True

	def _create_skip_stripes(self):
		"""Create skip stripe rectangles (memory-efficient lazy creation)"""
		if self.skip_stripes is None:
			# Create alternating yellow stripes: rows 0, 2, 4 = yellow (1px high each)
			stripe_color = dim_color(0xFFFF00)  # Yellow
			self.skip_stripes = [
				Rect(0, self.y, config['train_line_width'], 1, fill=stripe_color),     # Row 0
				Rect(0, self.y + 2, config['train_line_width'], 1, fill=stripe_color), # Row 2  
				Rect(0, self.y + 4, config['train_line_width'], 1, fill=stripe_color)  # Row 4
			]
			# Add to display group
			for stripe in self.skip_stripes:
				self.group.append(stripe)

	def _enable_skip_mode(self):
		"""Switch to skip mode with blinking stripes"""
		if not self.is_skip_mode:
			self.is_skip_mode = True
			self._create_skip_stripes()
			# Hide normal rectangles
			self.line_rect_top.hidden = True
			self.line_rect_bottom.hidden = True

	def _disable_skip_mode(self):
		"""Switch back to normal mode"""
		if self.is_skip_mode:
			self.is_skip_mode = False
			# Show normal rectangles
			self.line_rect_top.hidden = False
			self.line_rect_bottom.hidden = False
			# Hide stripes if they exist
			if self.skip_stripes:
				for stripe in self.skip_stripes:
					stripe.hidden = True


	def set_line_color(self, top_color: int, bottom_color: int = None, skip_mode: bool = False):
		if skip_mode:
			self._enable_skip_mode()
		else:
			self._disable_skip_mode()
			if bottom_color is not None:
				# Split-color mode
				self.line_rect_top.fill = top_color
				self.line_rect_bottom.fill = bottom_color
				self.line_rect_bottom.hidden = False
			else:
				# Solid-color mode
				self.line_rect_top.fill = top_color
				self.line_rect_bottom.hidden = True

	def set_destination(self, destination: str):
		self.destination_label.text = destination[:config['destination_max_characters']]

	def set_arrival_time(self, minutes: str):
		# Ensuring we have a string
		minutes = str(minutes)
		minutes_len = len(minutes)

		# Left-padding the minutes label
		minutes = ' ' * (config['min_label_characters'] - minutes_len) + minutes

		self.min_label.text = minutes

	def update(self, train_info: dict):
		self.show()
		line_color = train_info['line_color']
		destination = train_info['destination']
		minutes = train_info['arrival']

		# Transfer treatment keys on direction, not on a destination string: every
		# southbound train rides to Metro Center, whatever it terminates at.
		if train_info.get('southbound', False):
			if train_info.get('skip_mode', False):
				if train_info.get('skip_reason') == 'efficiency':
					# "Smart Skip": Next train gets same connection → blink yellow
					self.set_line_color(0, 0, skip_mode=True)
				else:
					# "No Data Skip": No connection visible → solid red
					self.set_line_color(dim_color(0xFF0000), dim_color(0xFF0000))
			else:
				# "Take" train: Red top, connection color bottom
				self.set_line_color(dim_color(0xFF0000), line_color)
		else:
			# Standard train (e.g., Shady Grove): Solid color bar
			self.set_line_color(line_color, line_color) # Set both halves to the same color

		self.set_destination(destination)
		self.set_arrival_time(minutes)
